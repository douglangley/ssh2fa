#!/usr/bin/env python3
"""Regression tests for RateLimiter: sliding windows, races, and scans."""

import json
import os
import tempfile
import threading
import unittest

from pam_ssh_2fa import RateLimiter


class RateLimiterWindowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.limiter = RateLimiter(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_allows_up_to_the_limit_then_denies(self):
        now = 1_000_000.0
        for _ in range(3):
            allowed, retry_after = self.limiter.check_window(
                "user", "alice", window_seconds=60, max_events=3, now=now
            )
            self.assertTrue(allowed)

        allowed, retry_after = self.limiter.check_window(
            "user", "alice", window_seconds=60, max_events=3, now=now
        )
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_window_slides_and_allows_again_after_expiry(self):
        now = 1_000_000.0
        for _ in range(3):
            self.limiter.check_window(
                "user", "bob", window_seconds=60, max_events=3, now=now
            )

        allowed, _ = self.limiter.check_window(
            "user", "bob", window_seconds=60, max_events=3, now=now + 1
        )
        self.assertFalse(allowed)

        # Past the window: the earlier events have aged out.
        allowed, _ = self.limiter.check_window(
            "user", "bob", window_seconds=60, max_events=3, now=now + 61
        )
        self.assertTrue(allowed)

    def test_different_identities_do_not_share_state(self):
        now = 1_000_000.0
        for _ in range(3):
            self.limiter.check_window(
                "user", "carol", window_seconds=60, max_events=3, now=now
            )

        # A different user, and the same user under a different kind
        # (source address), must not be affected by carol's usage.
        allowed, _ = self.limiter.check_window(
            "user", "dave", window_seconds=60, max_events=3, now=now
        )
        self.assertTrue(allowed)

        allowed, _ = self.limiter.check_window(
            "rhost", "carol", window_seconds=60, max_events=3, now=now
        )
        self.assertTrue(allowed)

    def test_denied_attempt_is_not_recorded(self):
        now = 1_000_000.0
        allowed, _ = self.limiter.check_window(
            "user", "erin", window_seconds=60, max_events=1, now=now
        )
        self.assertTrue(allowed)

        # This attempt is denied and must not consume a future slot.
        allowed, _ = self.limiter.check_window(
            "user", "erin", window_seconds=60, max_events=1, now=now
        )
        self.assertFalse(allowed)

        allowed, _ = self.limiter.check_window(
            "user", "erin", window_seconds=60, max_events=1, now=now + 61
        )
        self.assertTrue(allowed)

    def test_corrupt_state_file_is_treated_as_empty(self):
        path = self.limiter._state_file("user", "frank")
        with open(path, "w") as f:
            f.write("{not valid json")

        allowed, _ = self.limiter.check_window(
            "user", "frank", window_seconds=60, max_events=1, now=1_000_000.0
        )
        self.assertTrue(allowed)

    def test_concurrent_checks_never_exceed_the_limit(self):
        results = []
        lock = threading.Lock()

        def worker():
            allowed, _ = self.limiter.check_window(
                "user", "grace", window_seconds=60, max_events=5
            )
            with lock:
                results.append(allowed)

        threads = [threading.Thread(target=worker) for _ in range(25)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(results), 25)
        self.assertEqual(sum(1 for allowed in results if allowed), 5)


class RateLimiterConcurrencyScanTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.limiter = RateLimiter(os.path.join(self.tempdir.name, "ratelimit"))
        self.requests_dir = os.path.join(self.tempdir.name, "requests")
        os.makedirs(self.requests_dir)

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_request(self, name, user, expires):
        with open(os.path.join(self.requests_dir, name), "w") as f:
            json.dump({"user": user, "expires": expires}, f)

    def test_counts_only_matching_unexpired_requests(self):
        now = 1_000_000.0
        self._write_request("code_a.json", "alice", now + 60)
        self._write_request("code_b.json", "alice", now + 60)
        self._write_request("code_c.json", "alice", now - 1)  # expired
        self._write_request("code_d.json", "mallory", now + 60)  # different user

        count = self.limiter.count_active(
            self.requests_dir, "code_", "alice", now=now
        )
        self.assertEqual(count, 2)

    def test_ignores_files_without_matching_prefix(self):
        now = 1_000_000.0
        self._write_request("approval_x.json", "alice", now + 60)

        count = self.limiter.count_active(
            self.requests_dir, "code_", "alice", now=now
        )
        self.assertEqual(count, 0)

    def test_missing_directory_counts_as_zero(self):
        count = self.limiter.count_active(
            os.path.join(self.tempdir.name, "does-not-exist"), "code_", "alice"
        )
        self.assertEqual(count, 0)

    def test_corrupt_request_file_is_skipped(self):
        with open(os.path.join(self.requests_dir, "code_bad.json"), "w") as f:
            f.write("not json")

        count = self.limiter.count_active(self.requests_dir, "code_", "alice")
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
