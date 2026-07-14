#!/usr/bin/env python3
"""Security regression tests for the approval HTTP flow."""

import json
import os
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from approval_server import ApprovalManager, ApprovalServer


class ApprovalServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = ApprovalManager(self.tempdir.name)
        self.server = ApprovalServer("127.0.0.1", 0, self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def create_request(self, token="A_-" + "b" * 40):
        data = {
            "token": token,
            "confirmation_token": "C_-" + "d" * 40,
            "user": "<script>alert(1)</script>",
            "rhost": "192.0.2.1",
            "host": "example-host",
            "created": time.time(),
            "expires": time.time() + 300,
            "approved": False,
            "approved_at": None,
        }
        path = self.manager.get_approval_file(token)
        with open(path, "w", encoding="utf-8") as approval_file:
            json.dump(data, approval_file)
        os.chmod(path, 0o600)
        return data

    def request(self, path, data=None):
        body = None if data is None else urlencode(data).encode("ascii")
        request = Request(self.base_url + path, data=body)
        with urlopen(request, timeout=2) as response:
            return response.status, response.read().decode("utf-8"), response.headers

    def test_get_is_read_only_and_escapes_request_details(self):
        approval = self.create_request()

        status, body, headers = self.request(f"/approve/{approval['token']}")

        self.assertEqual(status, 200)
        self.assertIn("Approve SSH login", body)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertFalse(self.manager.get_approval(approval["token"])["approved"])

    def test_post_requires_confirmation_secret(self):
        approval = self.create_request()

        with self.assertRaises(HTTPError) as error:
            self.request(
                f"/approve/{approval['token']}",
                {"confirmation_token": "wrong-confirmation-token-value"},
            )

        self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.manager.get_approval(approval["token"])["approved"])

    def test_valid_post_approves_exact_token_with_dash_and_underscore(self):
        approval = self.create_request()

        status, body, _ = self.request(
            f"/approve/{approval['token']}",
            {"confirmation_token": approval["confirmation_token"]},
        )

        self.assertEqual(status, 200)
        self.assertIn("Access Approved", body)
        self.assertTrue(self.manager.get_approval(approval["token"])["approved"])

        with self.assertRaises(HTTPError) as replay_error:
            self.request(
                f"/approve/{approval['token']}",
                {"confirmation_token": approval["confirmation_token"]},
            )
        self.assertEqual(replay_error.exception.code, 409)

    def test_malformed_token_is_rejected_not_sanitized(self):
        self.create_request()
        with self.assertRaises(HTTPError) as error:
            self.request("/approve/../../etc/passwd")
        self.assertEqual(error.exception.code, 404)

    def test_health_endpoint_returns_valid_json(self):
        status, body, headers = self.request("/health")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("timestamp", payload)


if __name__ == "__main__":
    unittest.main()
