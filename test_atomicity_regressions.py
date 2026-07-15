#!/usr/bin/env python3
"""Regression tests for AUDIT_REMEDIATION_AND_ADMIN_PLAN.md P0-1/P0-2/P0-4/P0-5.

These were written against the audited revision (commit c04f8a7) to prove
each finding is a real, reproducible defect rather than a documentation
claim: RateLimiterReservationTests fails with AttributeError until
RateLimiter.reserve_request()/release_request() exist, and the other
classes fail their assertions against the pre-fix implementation. Once the
corresponding Phase 1 fix lands, every test in this file must pass.
"""

import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from approval_server import ApprovalRequestHandler
from pam_ssh_2fa import ApprovalManager, CodeManager, Config, RateLimiter

import pam_ssh_2fa
from test_authenticate_ratelimit import FakePamHandle


class CodeManagerAtomicityTests(unittest.TestCase):
    """P0-1: OTP validation must be atomic and single-use under concurrency."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = CodeManager(
            self.tempdir.name, code_length=6, timeout=60, max_attempts=3
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_concurrent_validation_of_same_code_yields_exactly_one_success(self):
        code, request_id = self.manager.generate("alice", "203.0.113.5")

        thread_count = 20
        barrier = threading.Barrier(thread_count)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            valid, _ = self.manager.validate("alice", "203.0.113.5", request_id, code)
            with lock:
                results.append(valid)

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(results), thread_count)
        successes = sum(1 for v in results if v)
        self.assertEqual(
            successes,
            1,
            f"expected exactly one successful validation of one OTP under "
            f"concurrency, got {successes} of {thread_count}",
        )

    def test_non_dict_json_state_fails_closed_instead_of_crashing(self):
        # Valid JSON that isn't an object (e.g. a bare array) must be
        # treated as corrupt state, not crash validate() with an
        # AttributeError from calling .get() on a list -- P1-6 in
        # AUDIT_REMEDIATION_AND_ADMIN_PLAN.md.
        _code, request_id = self.manager.generate("alice", "203.0.113.5")
        code_file = self.manager._get_code_file(request_id)
        with open(code_file, "w") as f:
            f.write('["not", "an", "object"]')

        valid, _message = self.manager.validate(
            "alice", "203.0.113.5", request_id, "000000"
        )
        self.assertFalse(valid)

    def test_non_numeric_expires_field_fails_closed_instead_of_crashing(self):
        # A corrupted or hand-edited state file with a non-numeric
        # "expires" must not crash the ">" comparison against time.time().
        _code, request_id = self.manager.generate("alice", "203.0.113.5")
        code_file = self.manager._get_code_file(request_id)
        with open(code_file, "r") as f:
            data = f.read()
        import json as _json

        parsed = _json.loads(data)
        parsed["expires"] = "not-a-number"
        with open(code_file, "w") as f:
            _json.dump(parsed, f)

        valid, _message = self.manager.validate(
            "alice", "203.0.113.5", request_id, "000000"
        )
        self.assertFalse(valid)

    def test_invalid_attempt_racing_a_valid_attempt_cannot_resurrect_state(self):
        code, request_id = self.manager.generate("alice", "203.0.113.5")

        barrier = threading.Barrier(2)

        def submit(entered):
            barrier.wait()
            self.manager.validate("alice", "203.0.113.5", request_id, entered)

        t_valid = threading.Thread(target=submit, args=(code,))
        t_invalid = threading.Thread(target=submit, args=("000000",))
        t_valid.start()
        t_invalid.start()
        t_valid.join(timeout=5)
        t_invalid.join(timeout=5)

        # Regardless of which side "won" the race, a later validation with
        # the correct code must never succeed again -- the request must
        # not be left in a state where it can be consumed twice.
        replay_valid, _ = self.manager.validate(
            "alice", "203.0.113.5", request_id, code
        )
        self.assertFalse(
            replay_valid,
            "OTP was resurrected/consumable after a concurrent valid/invalid race",
        )


class RateLimiterReservationTests(unittest.TestCase):
    """P0-2: the concurrent-request cap must be an atomic reservation, not
    a scan-then-create race, and one attempt must hold exactly one lease
    regardless of auth method."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.limiter = RateLimiter(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_reserve_request_never_exceeds_cap_under_concurrency(self):
        thread_count = 20
        cap = 3
        barrier = threading.Barrier(thread_count)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            lease_id = self.limiter.reserve_request("alice", ttl_seconds=60, limit=cap)
            with lock:
                results.append(lease_id)

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        granted = [r for r in results if r is not None]
        self.assertEqual(
            len(granted),
            cap,
            f"expected exactly {cap} reservations to succeed against a cap "
            f"of {cap}, got {len(granted)}",
        )
        self.assertEqual(len(set(granted)), cap)  # distinct lease ids

    def test_release_request_frees_a_slot(self):
        first = self.limiter.reserve_request("bob", ttl_seconds=60, limit=1)
        self.assertIsNotNone(first)
        self.assertIsNone(self.limiter.reserve_request("bob", ttl_seconds=60, limit=1))

        self.limiter.release_request("bob", first)

        second = self.limiter.reserve_request("bob", ttl_seconds=60, limit=1)
        self.assertIsNotNone(second)

    def test_expired_leases_do_not_count_against_the_cap(self):
        now = 1_000_000.0
        first = self.limiter.reserve_request("carol", ttl_seconds=30, limit=1, now=now)
        self.assertIsNotNone(first)

        self.assertIsNone(
            self.limiter.reserve_request(
                "carol", ttl_seconds=30, limit=1, now=now + 10
            )
        )

        third = self.limiter.reserve_request(
            "carol", ttl_seconds=30, limit=1, now=now + 31
        )
        self.assertIsNotNone(third)


class ConfigInterpolationTests(unittest.TestCase):
    """P0-4: valid percent-encoded notification URLs must not crash
    configuration loading."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_percent_encoded_apprise_url_does_not_crash_config_load(self):
        # A real ntfy/self-hosted URL can legitimately contain a
        # percent-encoded character (e.g. %40 for '@' in a query value).
        # configparser's default interpolation treats a bare '%' as the
        # start of an interpolation directive and raises
        # InterpolationSyntaxError from parser.get() -- which is not a
        # ValueError/TypeError, so it escapes Config's per-option
        # try/except and can abort the entire PAM authentication path.
        path = f"{self.tempdir.name}/config.ini"
        with open(path, "w") as f:
            f.write(
                "[notifications]\n"
                "apprise_urls = https://ntfy.example.com/topic?auth=abc%40def\n"
            )
        config = Config(path)  # must not raise
        self.assertIn("%40", config.get("apprise_urls"))


class ApprovalServerLoggingTests(unittest.TestCase):
    """P0-5: bearer tokens must never be logged, even in debug mode."""

    def test_debug_log_message_does_not_include_bearer_token(self):
        handler = object.__new__(ApprovalRequestHandler)
        handler.client_address = ("203.0.113.7", 51000)
        handler.command = "GET"
        secret_token = "S3CR3T_BEARER_TOKEN_VALUE_ABCDEFGH"
        handler.path = f"/approve/{secret_token}"
        request_line = f"{handler.command} {handler.path} HTTP/1.1"

        with self.assertLogs(level="DEBUG") as captured:
            handler.log_message('"%s" %s %s', request_line, "200", "-")

        combined = "\n".join(captured.output)
        self.assertNotIn(
            secret_token,
            combined,
            "approval bearer token leaked into the debug log via log_message()",
        )


class ApprovalConsumptionAtomicityTests(unittest.TestCase):
    """P1-1: approval grant-and-consume must be a single atomic operation."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = ApprovalManager(
            self.tempdir.name, timeout=60, server_url="https://approve.example.invalid"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_concurrent_consumption_of_one_approval_yields_exactly_one_success(self):
        token, _link = self.manager.create_approval("alice", "203.0.113.5")
        approval_file = self.manager._get_approval_file(token)
        with open(approval_file, "r") as f:
            data = f.read()
        data = data.replace('"approved": false', '"approved": true')
        with open(approval_file, "w") as f:
            f.write(data)

        thread_count = 20
        barrier = threading.Barrier(thread_count)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            with lock:
                results.append(self.manager.consume_approval(token))

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(results), thread_count)
        successes = sum(1 for v in results if v)
        self.assertEqual(
            successes,
            1,
            f"expected exactly one successful consumption of one approval "
            f"under concurrency, got {successes} of {thread_count}",
        )


class RequestStateCleanupTests(unittest.TestCase):
    """P0-3: every exit path must clean up whatever request state it
    created, not just the paths that already had explicit cleanup calls.

    Before the fix, a notification-delivery failure in "both" mode only
    cleaned up the approval file -- the OTP code file (created first, in
    the same attempt) was left behind on disk.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        config_path = f"{self.tempdir.name}/config.ini"
        self.storage_dir = f"{self.tempdir.name}/state"
        with open(config_path, "w") as f:
            f.write(
                f"""
[general]
log_file = {self.tempdir.name}/test.log

[codes]
length = 6
max_attempts = 3
storage_dir = {self.storage_dir}

[notifications]
apprise_urls = json://example.invalid/hook

[server]
url = https://approvals.example.invalid

[users]
auth_method = both

[ratelimit]
window = 300
max_per_user = 100
max_per_rhost = 100
max_concurrent_per_user = 3
"""
            )

        self.config_patch = patch.object(pam_ssh_2fa, "CONFIG_FILE", config_path)
        self.config_patch.start()

        self.send_patch = patch.object(
            pam_ssh_2fa.NotificationSender, "send", return_value=False
        )
        self.send_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.send_patch.stop()
        self.tempdir.cleanup()

    def _state_files(self):
        # Only look at OTP code files and approval-request files -- not
        # the ratelimit/ subdirectory, which legitimately keeps
        # persistent sliding-window and lease-accounting counters
        # regardless of how any single authentication attempt ends.
        found = []
        if os.path.isdir(self.storage_dir):
            for name in os.listdir(self.storage_dir):
                if name.startswith("code_") and name.endswith(".json"):
                    found.append(os.path.join(self.storage_dir, name))
        approvals_dir = os.path.join(self.storage_dir, "approvals")
        if os.path.isdir(approvals_dir):
            for name in os.listdir(approvals_dir):
                if name.endswith(".json"):
                    found.append(os.path.join(approvals_dir, name))
        return found

    def test_notification_failure_cleans_up_both_code_and_approval_state(self):
        pamh = FakePamHandle("alice", "203.0.113.9")
        result = pam_ssh_2fa.pam_sm_authenticate(pamh, 0, [])

        self.assertEqual(result, pam_ssh_2fa.PAM_AUTHINFO_UNAVAIL)
        self.assertEqual(
            self._state_files(),
            [],
            "notification failure left OTP/approval state behind on disk",
        )


if __name__ == "__main__":
    unittest.main()
