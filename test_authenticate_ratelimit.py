#!/usr/bin/env python3
"""End-to-end check that pam_sm_authenticate() enforces rate limits.

Unlike test_rate_limiter.py (which exercises RateLimiter directly),
this drives the real pam_sm_authenticate() entry point through a fake
PAM handle to confirm the gate is actually wired into the auth flow
and returns PAM_MAXTRIES before any code/notification is generated.
"""

import tempfile
import unittest
from unittest.mock import patch

import pam_ssh_2fa
from pam_ssh_2fa import PAM_AUTH_ERR, PAM_MAXTRIES


class FakePamException(Exception):
    pass


class FakeMessage:
    def __init__(self, style, msg):
        self.style = style
        self.msg = msg


class FakeResponse:
    def __init__(self, resp):
        self.resp = resp


class FakePamHandle:
    """Minimal stand-in for the pam_python handle object."""

    exception = FakePamException
    Message = FakeMessage

    PAM_PROMPT_ECHO_ON = 1
    PAM_PROMPT_ECHO_OFF = 2
    PAM_TEXT_INFO = 3
    PAM_ERROR_MSG = 4

    def __init__(self, user, rhost, code_responses=None):
        self._user = user
        self.rhost = rhost
        self._code_responses = list(code_responses or [])
        self.error_messages = []
        self.info_messages = []

    def get_user(self, prompt):
        return self._user

    def conversation(self, message):
        if message.style in (self.PAM_PROMPT_ECHO_ON, self.PAM_PROMPT_ECHO_OFF):
            value = self._code_responses.pop(0) if self._code_responses else ""
            return FakeResponse(value)
        if message.style == self.PAM_ERROR_MSG:
            self.error_messages.append(message.msg)
        elif message.style == self.PAM_TEXT_INFO:
            self.info_messages.append(message.msg)
        return None


class AuthenticateRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        config_path = f"{self.tempdir.name}/config.ini"
        with open(config_path, "w") as f:
            f.write(
                f"""
[general]
log_file = {self.tempdir.name}/test.log

[codes]
length = 6
max_attempts = 1
storage_dir = {self.tempdir.name}/state

[notifications]
apprise_urls = json://example.invalid/hook

[ratelimit]
window = 300
max_per_user = 2
max_per_rhost = 100
max_concurrent_per_user = 100
"""
            )

        self.config_patch = patch.object(pam_ssh_2fa, "CONFIG_FILE", config_path)
        self.config_patch.start()

        # Isolate from real Apprise/network delivery -- the rate limiter
        # gate runs before notification send, so allowed attempts still
        # need send() to return without touching the network.
        self.send_patch = patch.object(
            pam_ssh_2fa.NotificationSender, "send", return_value=True
        )
        self.send_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.send_patch.stop()
        self.tempdir.cleanup()

    def test_third_attempt_within_window_is_rate_limited(self):
        # max_per_user is 2: the first two authentication attempts should
        # run the normal code flow (and fail on a wrong code, since we
        # don't know the generated one); the third must be rejected by
        # the rate limiter before any code is even generated.
        for _ in range(2):
            pamh = FakePamHandle("alice", "203.0.113.9", code_responses=["000000"])
            result = pam_ssh_2fa.pam_sm_authenticate(pamh, 0, [])
            self.assertEqual(result, PAM_AUTH_ERR)

        pamh = FakePamHandle("alice", "203.0.113.9", code_responses=["000000"])
        result = pam_ssh_2fa.pam_sm_authenticate(pamh, 0, [])

        self.assertEqual(result, PAM_MAXTRIES)
        self.assertTrue(
            any("too many" in msg.lower() for msg in pamh.error_messages),
            pamh.error_messages,
        )

    def test_rate_limit_does_not_affect_other_users(self):
        for _ in range(2):
            pamh = FakePamHandle("alice", "203.0.113.9", code_responses=["000000"])
            pam_ssh_2fa.pam_sm_authenticate(pamh, 0, [])

        # alice is now rate limited, but bob from a different address
        # must be unaffected.
        pamh = FakePamHandle("bob", "198.51.100.4", code_responses=["000000"])
        result = pam_ssh_2fa.pam_sm_authenticate(pamh, 0, [])
        self.assertEqual(result, PAM_AUTH_ERR)


if __name__ == "__main__":
    unittest.main()
