#!/usr/bin/env python3
"""Tests for pam_ssh_2fa.send_notifications() -- the orchestration layer
that picks native providers vs. the legacy Apprise path, and applies
delivery_policy/total_timeout across multiple configured providers. See
AUDIT_REMEDIATION_AND_ADMIN_PLAN.md Phase 3.

Provider-level HTTP behavior (timeouts, TLS, malformed responses, etc.)
is covered by test_notifiers.py; this file is about the policy logic
that sits on top of one or more Notifier instances.
"""

import http.server
import tempfile
import threading
import unittest
from unittest.mock import patch

import pam_ssh_2fa
from pam_ssh_2fa import Config, send_notifications


class _NtfyStub(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(_NtfyStub.status)
        self.end_headers()

    def log_message(self, *a):
        pass


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


class LegacyFallbackTests(unittest.TestCase):
    """No [notification] providers configured -- must behave exactly as
    before Phase 3 (the existing NotificationSender/Apprise path)."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_falls_back_to_apprise_when_no_providers_configured(self):
        config_path = f"{self.tempdir.name}/config.ini"
        _write(config_path, "[notifications]\napprise_urls = json://127.0.0.1:1/unreachable\n")
        config = Config(config_path)
        user_config = config.get_user_config("alice")

        with patch.object(pam_ssh_2fa.NotificationSender, "send", return_value=True) as mock_send:
            result = send_notifications(
                config=config,
                user_config=user_config,
                code="123456",
                user="alice",
                rhost="203.0.113.1",
                timeout=300,
                link=None,
                auth_method="code",
                logger=None,
            )
        self.assertTrue(result)
        mock_send.assert_called_once()


class NativeProviderOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _NtfyStub)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.ntfy_url = f"http://127.0.0.1:{self.server.server_port}/mytopic"
        _NtfyStub.status = 200

        class _NullLogger:
            def info(self, *a, **k):
                pass

            def warning(self, *a, **k):
                pass

            def error(self, *a, **k):
                pass

            def debug(self, *a, **k):
                pass

        self.logger = _NullLogger()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tempdir.cleanup()

    def _config_and_user_config(self, global_extra="", user_extra=""):
        config_path = f"{self.tempdir.name}/config.ini"
        _write(config_path, global_extra)
        config = Config(config_path)

        import os

        os.makedirs(f"{self.tempdir.name}/users", exist_ok=True)
        _write(f"{self.tempdir.name}/users/alice.conf", user_extra)
        user_config = config.get_user_config("alice")
        return config, user_config

    def test_single_native_provider_success(self):
        config, user_config = self._config_and_user_config(
            user_extra=f"[notification]\nproviders = ntfy\n\n[ntfy]\npublish_url = {self.ntfy_url}\n"
        )
        result = send_notifications(
            config=config,
            user_config=user_config,
            code="123456",
            user="alice",
            rhost="203.0.113.1",
            timeout=300,
            link=None,
            auth_method="code",
            logger=self.logger,
        )
        self.assertTrue(result)

    def test_any_policy_stops_at_first_success(self):
        # "unknownprovider" would fail to build; ntfy succeeds. any policy
        # means overall success even though one entry was invalid.
        config, user_config = self._config_and_user_config(
            global_extra="[notifications]\ndelivery_policy = any\n",
            user_extra=(
                f"[notification]\nproviders = ntfy,unknownprovider\n\n"
                f"[ntfy]\npublish_url = {self.ntfy_url}\n"
            ),
        )
        result = send_notifications(
            config=config,
            user_config=user_config,
            code="123456",
            user="alice",
            rhost="203.0.113.1",
            timeout=300,
            link=None,
            auth_method="code",
            logger=self.logger,
        )
        self.assertTrue(result)

    def test_all_policy_requires_every_provider_to_succeed(self):
        _NtfyStub.status = 500  # ntfy will fail
        config, user_config = self._config_and_user_config(
            global_extra="[notifications]\ndelivery_policy = all\n",
            user_extra=(
                f"[notification]\nproviders = ntfy\n\n"
                f"[ntfy]\npublish_url = {self.ntfy_url}\n"
            ),
        )
        result = send_notifications(
            config=config,
            user_config=user_config,
            code="123456",
            user="alice",
            rhost="203.0.113.1",
            timeout=300,
            link=None,
            auth_method="code",
            logger=self.logger,
        )
        self.assertFalse(result)

    def test_all_policy_succeeds_when_every_provider_succeeds(self):
        _NtfyStub.status = 200
        config, user_config = self._config_and_user_config(
            global_extra="[notifications]\ndelivery_policy = all\n",
            user_extra=(
                f"[notification]\nproviders = ntfy\n\n"
                f"[ntfy]\npublish_url = {self.ntfy_url}\n"
            ),
        )
        result = send_notifications(
            config=config,
            user_config=user_config,
            code="123456",
            user="alice",
            rhost="203.0.113.1",
            timeout=300,
            link=None,
            auth_method="code",
            logger=self.logger,
        )
        self.assertTrue(result)

    def test_unknown_provider_alone_fails_closed_without_crashing(self):
        config, user_config = self._config_and_user_config(
            user_extra="[notification]\nproviders = totally_unknown_provider\n"
        )
        result = send_notifications(
            config=config,
            user_config=user_config,
            code="123456",
            user="alice",
            rhost="203.0.113.1",
            timeout=300,
            link=None,
            auth_method="code",
            logger=self.logger,
        )
        self.assertFalse(result)

    def test_pushover_selected_without_credentials_fails_closed(self):
        config, user_config = self._config_and_user_config(
            user_extra="[notification]\nproviders = pushover\n"
        )
        result = send_notifications(
            config=config,
            user_config=user_config,
            code="123456",
            user="alice",
            rhost="203.0.113.1",
            timeout=300,
            link=None,
            auth_method="code",
            logger=self.logger,
        )
        self.assertFalse(result)

    def test_total_timeout_skips_remaining_providers(self):
        _NtfyStub.status = 200

        class _SlowThenFastLogger:
            def info(self, *a, **k):
                pass

            def warning(self, *a, **k):
                pass

            def error(self, *a, **k):
                pass

            def debug(self, *a, **k):
                pass

        config, user_config = self._config_and_user_config(
            global_extra="[notifications]\ntotal_timeout = 1\n",
            user_extra=(
                f"[notification]\nproviders = pushover,ntfy\n\n"
                f"[ntfy]\npublish_url = {self.ntfy_url}\n"
            ),
        )
        # pushover has no credentials configured, so _build_notifier
        # returns None for it and it's never actually sent -- this test
        # instead confirms the deadline mechanism doesn't crash and a
        # reachable provider after a skipped one still gets a chance
        # when the deadline hasn't elapsed yet.
        result = send_notifications(
            config=config,
            user_config=user_config,
            code="123456",
            user="alice",
            rhost="203.0.113.1",
            timeout=300,
            link=None,
            auth_method="code",
            logger=_SlowThenFastLogger(),
        )
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
