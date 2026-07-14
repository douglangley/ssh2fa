#!/usr/bin/env python3
"""Security and concurrency regression tests for OTP code state."""

import tempfile
import threading
import unittest

from pam_ssh_2fa import CodeManager


class CodeManagerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = CodeManager(
            self.tempdir.name, code_length=4, timeout=60, max_attempts=3
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_simultaneous_attempts_from_same_user_and_source_do_not_collide(self):
        # Two SSH connections from the same user behind the same NAT
        # address must not be able to read or invalidate each other's code.
        code_a, request_a = self.manager.generate("alice", "203.0.113.5")
        code_b, request_b = self.manager.generate("alice", "203.0.113.5")

        self.assertNotEqual(request_a, request_b)
        self.assertNotEqual(code_a, code_b)

        valid_a, _ = self.manager.validate("alice", "203.0.113.5", request_a, code_a)
        valid_b, _ = self.manager.validate("alice", "203.0.113.5", request_b, code_b)
        self.assertTrue(valid_a)
        self.assertTrue(valid_b)

    def test_wrong_request_id_is_rejected(self):
        code, request_id = self.manager.generate("alice", "203.0.113.5")
        _, other_request_id = self.manager.generate("alice", "203.0.113.5")

        valid, message = self.manager.validate(
            "alice", "203.0.113.5", other_request_id, code
        )
        self.assertFalse(valid)

    def test_request_id_is_bound_to_user_and_rhost(self):
        code, request_id = self.manager.generate("alice", "203.0.113.5")

        valid, message = self.manager.validate(
            "mallory", "203.0.113.5", request_id, code
        )
        self.assertFalse(valid)
        self.assertIn("Session mismatch", message)

        valid, message = self.manager.validate(
            "alice", "198.51.100.9", request_id, code
        )
        self.assertFalse(valid)

    def test_malformed_request_id_is_rejected_not_sanitized(self):
        valid, message = self.manager.validate(
            "alice", "203.0.113.5", "../../etc/passwd", "1234"
        )
        self.assertFalse(valid)

    def test_code_is_single_use(self):
        code, request_id = self.manager.generate("alice", "203.0.113.5")

        valid, _ = self.manager.validate("alice", "203.0.113.5", request_id, code)
        self.assertTrue(valid)

        replay_valid, _ = self.manager.validate(
            "alice", "203.0.113.5", request_id, code
        )
        self.assertFalse(replay_valid)

    def test_concurrent_generate_and_validate_is_race_free(self):
        errors = []
        results = []
        lock = threading.Lock()

        def worker():
            try:
                code, request_id = self.manager.generate("bob", "192.0.2.1")
                valid, _ = self.manager.validate(
                    "bob", "192.0.2.1", request_id, code
                )
                with lock:
                    results.append(valid)
            except Exception as exc:  # pragma: no cover - failure path
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(25)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 25)
        self.assertTrue(all(results))


if __name__ == "__main__":
    unittest.main()
