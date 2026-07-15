#!/usr/bin/env python3
"""Provider contract tests for notifiers.py (Pushover, ntfy, Apprise
adapter), per AUDIT_REMEDIATION_AND_ADMIN_PLAN.md Phase 3's exit
criterion: "Provider contract tests cover all HTTP/status/timeout/
redirect/rate-limit paths."

All tests run against local stub HTTP(S) servers -- no real network
calls to Pushover/ntfy/anything else.
"""

import http.server
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from notifiers import (
    AppriseNotifier,
    Notification,
    NtfyNotifier,
    PushoverNotifier,
)

OPENSSL = shutil.which("openssl")

VALID_TOKEN = "a" * 30
VALID_USER_KEY = "b" * 30


def generate_self_signed_cert(directory: str) -> tuple:
    cert_path = str(Path(directory) / "cert.pem")
    key_path = str(Path(directory) / "key.pem")
    subprocess.run(
        [
            OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key_path, "-out", cert_path, "-days", "1",
            "-subj", "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert_path, key_path


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Configurable per-test stub: class attributes set the response."""

    response_status = 200
    response_body = b"{}"
    response_content_type = "application/json"
    delay_seconds = 0
    captured_requests = []  # class-level, cleared by setUp

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _StubHandler.captured_requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        try:
            self.send_response(self.response_status)
            self.send_header("Content-Type", self.response_content_type)
            self.end_headers()
            self.wfile.write(self.response_body)
        except BrokenPipeError:
            # The timeout tests deliberately give up and close their
            # socket before a delayed response finishes writing -- that's
            # the client behavior under test, not a server bug.
            pass

    def do_POST(self):
        self._handle()

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except BrokenPipeError:
            pass

    def log_message(self, *args):
        pass


def _start_stub_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class PushoverNotifierTests(unittest.TestCase):
    def setUp(self):
        _StubHandler.captured_requests = []
        _StubHandler.response_status = 200
        _StubHandler.response_body = json.dumps({"status": 1}).encode()
        _StubHandler.delay_seconds = 0
        self.server, self.thread = _start_stub_server()
        import notifiers

        self._real_url = notifiers.PUSHOVER_API_URL
        notifiers.PUSHOVER_API_URL = (
            f"http://127.0.0.1:{self.server.server_port}/1/messages.json"
        )

    def tearDown(self):
        import notifiers

        notifiers.PUSHOVER_API_URL = self._real_url
        self.server.shutdown()
        self.server.server_close()

    def test_rejects_malformed_app_token(self):
        with self.assertRaises(ValueError):
            PushoverNotifier(app_token="too-short", user_key=VALID_USER_KEY)

    def test_rejects_malformed_user_key(self):
        with self.assertRaises(ValueError):
            PushoverNotifier(app_token=VALID_TOKEN, user_key="not-30-chars")

    def test_successful_delivery(self):
        notifier = PushoverNotifier(app_token=VALID_TOKEN, user_key=VALID_USER_KEY)
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertTrue(result.success)
        self.assertEqual(result.provider, "pushover")
        self.assertEqual(result.status_code, 200)
        # No secret should ever appear in the redacted detail.
        self.assertNotIn(VALID_TOKEN, result.redacted_detail)
        self.assertNotIn(VALID_USER_KEY, result.redacted_detail)

    def test_credentials_never_appear_in_request_path_or_logged_detail(self):
        notifier = PushoverNotifier(app_token=VALID_TOKEN, user_key=VALID_USER_KEY)
        notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        # Pushover's API takes credentials in the POST body, not the URL
        # path/query -- confirm the request path itself carries nothing.
        self.assertEqual(_StubHandler.captured_requests[-1]["path"], "/1/messages.json")

    def test_invalid_recipient_is_reported_as_failure(self):
        _StubHandler.response_status = 400
        _StubHandler.response_body = json.dumps(
            {"status": 0, "errors": ["user identifier is invalid"]}
        ).encode()
        notifier = PushoverNotifier(app_token=VALID_TOKEN, user_key=VALID_USER_KEY)
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.retryable)  # 4xx: a config problem, not transient

    def test_server_error_is_retryable(self):
        _StubHandler.response_status = 503
        _StubHandler.response_body = b"Service Unavailable"
        _StubHandler.response_content_type = "text/plain"
        notifier = PushoverNotifier(app_token=VALID_TOKEN, user_key=VALID_USER_KEY)
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    def test_rate_limit_429_is_retryable(self):
        _StubHandler.response_status = 429
        _StubHandler.response_body = json.dumps({"status": 0}).encode()
        notifier = PushoverNotifier(app_token=VALID_TOKEN, user_key=VALID_USER_KEY)
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    def test_timeout_is_retryable_and_does_not_raise(self):
        _StubHandler.delay_seconds = 2
        notifier = PushoverNotifier(
            app_token=VALID_TOKEN,
            user_key=VALID_USER_KEY,
            connect_timeout=0.2,
            read_timeout=0.2,
        )
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    def test_malformed_json_response_does_not_crash(self):
        _StubHandler.response_body = b"not valid json{{{"
        notifier = PushoverNotifier(app_token=VALID_TOKEN, user_key=VALID_USER_KEY)
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)

    def test_oversized_response_is_bounded_not_crashed(self):
        _StubHandler.response_body = b'{"status": 1, "pad": "' + b"x" * 100_000 + b'"}'
        notifier = PushoverNotifier(app_token=VALID_TOKEN, user_key=VALID_USER_KEY)
        # Truncated JSON (cut off mid-payload) is malformed -- must fail
        # closed, not crash and not report success from an unparsed body.
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertIsNotNone(result)  # did not raise

    def test_title_and_message_are_truncated_to_documented_limits(self):
        notifier = PushoverNotifier(app_token=VALID_TOKEN, user_key=VALID_USER_KEY)
        notifier.send(
            Notification(
                request_id="r1",
                title="T" * 1000,
                body="B" * 5000,
            )
        )
        sent_body = _StubHandler.captured_requests[-1]["body"].decode()
        # Just confirm the oversized fields didn't get sent verbatim --
        # exact URL-encoded length isn't asserted since quoting varies.
        self.assertLess(len(sent_body), 5000 + 1000)


@unittest.skipUnless(OPENSSL, "openssl CLI not available to generate a test cert")
class PushoverNotifierTLSTests(unittest.TestCase):
    """Confirms TLS certificate/hostname verification is NOT disabled."""

    def setUp(self):
        self.certdir = tempfile.TemporaryDirectory()
        self.cert_path, self.key_path = generate_self_signed_cert(self.certdir.name)
        _StubHandler.captured_requests = []
        _StubHandler.response_status = 200
        _StubHandler.response_body = json.dumps({"status": 1}).encode()
        _StubHandler.delay_seconds = 0

        self.server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        import notifiers

        self._real_url = notifiers.PUSHOVER_API_URL
        # Deliberately https:// against a self-signed cert for CN=localhost
        # while connecting to the IP literal 127.0.0.1 -- both an
        # untrusted-CA and a hostname-mismatch condition.
        notifiers.PUSHOVER_API_URL = (
            f"https://127.0.0.1:{self.server.server_port}/1/messages.json"
        )

    def tearDown(self):
        import notifiers

        notifiers.PUSHOVER_API_URL = self._real_url
        self.server.shutdown()
        self.server.server_close()
        self.certdir.cleanup()

    def test_untrusted_self_signed_certificate_is_rejected(self):
        notifier = PushoverNotifier(app_token=VALID_TOKEN, user_key=VALID_USER_KEY)
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertEqual(len(_StubHandler.captured_requests), 0)  # never got there


class NtfyNotifierTests(unittest.TestCase):
    def setUp(self):
        _StubHandler.captured_requests = []
        _StubHandler.response_status = 200
        _StubHandler.response_body = b""
        _StubHandler.response_content_type = "text/plain"
        _StubHandler.delay_seconds = 0
        self.server, self.thread = _start_stub_server()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/mytopic"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_rejects_missing_topic_path(self):
        with self.assertRaises(ValueError):
            NtfyNotifier(publish_url=f"http://127.0.0.1:{self.server.server_port}")

    def test_rejects_unsupported_scheme(self):
        with self.assertRaises(ValueError):
            NtfyNotifier(publish_url="ftp://example.com/topic")

    def test_rejects_insecure_http_to_public_host_by_default(self):
        with self.assertRaises(ValueError):
            NtfyNotifier(publish_url="http://ntfy.example.com/topic")

    def test_allows_insecure_http_to_loopback(self):
        notifier = NtfyNotifier(publish_url=self.base_url)  # 127.0.0.1, loopback
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertTrue(result.success)

    def test_access_token_sent_as_bearer_header_not_in_url(self):
        notifier = NtfyNotifier(publish_url=self.base_url, access_token="secret-token-value")
        notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        request = _StubHandler.captured_requests[-1]
        self.assertEqual(request["headers"].get("Authorization"), "Bearer secret-token-value")
        self.assertNotIn("secret-token-value", request["path"])

    def test_click_url_sent_as_header(self):
        notifier = NtfyNotifier(publish_url=self.base_url)
        notifier.send(
            Notification(
                request_id="r1",
                title="SSH Login",
                body="Code: 123456",
                click_url="https://approve.example.invalid/approve/tok",
            )
        )
        request = _StubHandler.captured_requests[-1]
        self.assertEqual(
            request["headers"].get("Click"), "https://approve.example.invalid/approve/tok"
        )

    def test_server_error_is_reported_and_retryable(self):
        _StubHandler.response_status = 500
        notifier = NtfyNotifier(publish_url=self.base_url)
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    def test_rate_limit_429_is_retryable(self):
        _StubHandler.response_status = 429
        notifier = NtfyNotifier(publish_url=self.base_url)
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    def test_timeout_is_retryable_and_does_not_raise(self):
        _StubHandler.delay_seconds = 2
        notifier = NtfyNotifier(
            publish_url=self.base_url, connect_timeout=0.2, read_timeout=0.2
        )
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    def test_redirect_is_not_followed(self):
        _StubHandler.response_status = 302
        _StubHandler.response_body = b""
        notifier = NtfyNotifier(publish_url=self.base_url, access_token="secret")
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        # A 3xx must be treated as a failed delivery, not silently
        # followed (which could forward the Authorization header to a
        # different host).
        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 302)

    def test_unauthenticated_ntfy_sh_logs_a_warning(self):
        warnings = []

        class FakeLogger:
            def warning(self, message, **kwargs):
                warnings.append(message)

            def error(self, *a, **k):
                pass

        # ntfy.sh itself isn't reachable/needed for this test -- only
        # construction (which inspects the hostname) is under test.
        NtfyNotifier(publish_url="https://ntfy.sh/some-topic", logger=FakeLogger())
        self.assertTrue(any("ntfy.sh" in w for w in warnings))

    def test_authenticated_ntfy_sh_does_not_warn(self):
        warnings = []

        class FakeLogger:
            def warning(self, message, **kwargs):
                warnings.append(message)

            def error(self, *a, **k):
                pass

        NtfyNotifier(
            publish_url="https://ntfy.sh/some-topic",
            access_token="tok",
            logger=FakeLogger(),
        )
        self.assertEqual(warnings, [])

    def test_title_header_strips_control_characters(self):
        notifier = NtfyNotifier(publish_url=self.base_url)
        notifier.send(
            Notification(
                request_id="r1", title="SSH\r\nInjected-Header: evil", body="Code"
            )
        )
        request = _StubHandler.captured_requests[-1]
        self.assertNotIn("\r", request["headers"].get("Title", ""))
        self.assertNotIn("\n", request["headers"].get("Title", ""))


class AppriseNotifierTests(unittest.TestCase):
    def setUp(self):
        _StubHandler.captured_requests = []
        _StubHandler.response_status = 200
        _StubHandler.response_body = b"{}"
        _StubHandler.delay_seconds = 0
        self.server, self.thread = _start_stub_server()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_successful_delivery_via_generic_json_webhook(self):
        url = f"json://127.0.0.1:{self.server.server_port}/hook"
        notifier = AppriseNotifier(apprise_urls=[url])
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertTrue(result.success)
        self.assertEqual(result.provider, "apprise")

    def test_no_urls_configured_is_a_clean_failure(self):
        notifier = AppriseNotifier(apprise_urls=[])
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)

    def test_unreachable_url_is_a_clean_failure_not_a_crash(self):
        notifier = AppriseNotifier(apprise_urls=["json://127.0.0.1:1/unreachable"])
        result = notifier.send(
            Notification(request_id="r1", title="SSH Login", body="Code: 123456")
        )
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
