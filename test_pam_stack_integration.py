#!/usr/bin/env python3
"""System-level integration tests driving the real PAM module through
pamtester, using the module's *actual* PAM entry point (pam_python.so),
not just its Python API.

These exist because a pure-Python unit test cannot see PAM control-flow
bugs: PAM_IGNORE vs PAM_SUCCESS, whether an included @include common-auth
silently demands a Unix password before this module ever runs, whether a
password-locked (SSH-key-only) account can authenticate at all. Those
questions can only be answered by actually invoking libpam against a real
stack definition. See MODERNIZATION_PLAN.md's "the documented PAM stack
may not match the promised flow" finding -- this file is the regression
suite for it, and for the PAM_IGNORE bypass bug found while investigating
it (bypass_checker.should_bypass() returning PAM_IGNORE denied a bypassed
user's login instead of granting it, once the stack no longer has another
module to fall back on -- see the fix in pam_sm_authenticate()).

Requires root, `pamtester`, and libpam-python (for pam_python.so). Skips
itself cleanly if any precondition isn't met, and -- critically -- also
skips if a real install already exists at INSTALL_DIR, so this never
touches a genuine deployment's config.
"""

import http.server
import json
import os
import pwd
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

INSTALL_DIR = "/etc/pam-ssh-2fa"
PAM_SERVICE_NAME = "pam-ssh-2fa-itest"
PAM_SERVICE_FILE = f"/etc/pam.d/{PAM_SERVICE_NAME}"
TEST_USER = "pamssh2fa_itest_user"

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

PAMTESTER = shutil.which("pamtester")
IS_ROOT = os.geteuid() == 0
PRE_EXISTING_INSTALL = os.path.isdir(INSTALL_DIR)


def _skip_reason():
    if not IS_ROOT:
        return "requires root"
    if not PAMTESTER:
        return "pamtester is not installed"
    if PRE_EXISTING_INSTALL:
        return f"{INSTALL_DIR} already exists -- refusing to touch a real install"
    return None


class _NotificationStubHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for a real push provider: accepts any POST and returns
    200, so NotificationSender.send() succeeds without a network call to
    an actual notification service."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def _run_pamtester(user, responses, timeout=5):
    """
    Drive `pamtester <service> <user> authenticate`, feeding each of
    `responses` as a line of input whenever the module reads a real
    generated code from storage isn't needed (caller already knows the
    value to send). Returns (returncode, combined_output).
    """
    proc = subprocess.run(
        [PAMTESTER, PAM_SERVICE_NAME, user, "authenticate"],
        input="\n".join(responses) + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _wait_for_code(storage_dir, timeout=3):
    """Poll storage_dir for a freshly generated OTP code file and return
    the plaintext code. The PAM conversation is synchronous from the
    caller's side, so the code is written before pamtester's prompt
    reaches us; a short poll absorbs process-scheduling jitter."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = [f for f in os.listdir(storage_dir) if f.startswith("code_")]
        if matches:
            with open(os.path.join(storage_dir, matches[0])) as f:
                return json.load(f)["code"]
        time.sleep(0.05)
    raise TimeoutError("no code file appeared in storage dir")


@unittest.skipIf(_skip_reason(), _skip_reason() or "")
class PamStackIntegrationTests(unittest.TestCase):
    """
    Exercises the RECOMMENDED PAM stack (see examples/pam.d-sshd.example):
    pam_python.so as the sole auth module, with no @include common-auth.
    This is deliberate -- SSH key verification already happened at the
    OpenSSH layer before this stack is ever invoked, and empirically
    (via this exact test file, run manually against both variants while
    developing the fix) including common-auth here:
      - silently requires a valid Unix password before 2FA is attempted
      - makes login completely impossible for password-locked
        (SSH-key-only) accounts, which is the deployment this module
        primarily targets
    """

    @classmethod
    def setUpClass(cls):
        os.makedirs(INSTALL_DIR, exist_ok=True)
        shutil.copy(
            os.path.join(REPO_DIR, "pam_ssh_2fa.py"),
            os.path.join(INSTALL_DIR, "pam_ssh_2fa.py"),
        )

        with open(PAM_SERVICE_FILE, "w") as f:
            f.write(
                "# Created by test_pam_stack_integration.py -- safe to delete\n"
                "auth required pam_python.so "
                f"{INSTALL_DIR}/pam_ssh_2fa.py\n"
                "account required pam_permit.so\n"
            )

        try:
            pwd.getpwnam(TEST_USER)
        except KeyError:
            subprocess.run(
                ["useradd", "-M", "-s", "/usr/sbin/nologin", TEST_USER], check=True
            )
        # Lock the password: this project's target deployment is
        # SSH-key-only, so the regression test that matters most is
        # "can a password-locked account still get through 2FA".
        subprocess.run(["passwd", "-l", TEST_USER], check=True, capture_output=True)

        # Stub notification endpoint so tests that need delivery to
        # actually succeed (and reach the "enter code" prompt) don't
        # depend on a real push provider.
        cls.notify_server = http.server.HTTPServer(
            ("127.0.0.1", 0), _NotificationStubHandler
        )
        cls.notify_url = f"json://127.0.0.1:{cls.notify_server.server_port}/hook"
        cls.notify_thread = threading.Thread(
            target=cls.notify_server.serve_forever, daemon=True
        )
        cls.notify_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.notify_server.shutdown()
        cls.notify_server.server_close()
        subprocess.run(["userdel", "-f", TEST_USER], capture_output=True)
        try:
            os.remove(PAM_SERVICE_FILE)
        except OSError:
            pass
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)

    def setUp(self):
        # Fresh storage dir and config per test, so rate-limit counters
        # and leftover code files from one test can never bleed into
        # the next (all tests share TEST_USER).
        self.storage_dir = tempfile.mkdtemp(prefix="pamssh2fa-itest-storage-")
        os.chmod(self.storage_dir, 0o700)
        self.config_path = os.path.join(INSTALL_DIR, "config.ini")

    def tearDown(self):
        shutil.rmtree(self.storage_dir, ignore_errors=True)

    def _write_config(self, extra=""):
        with open(self.config_path, "w") as f:
            f.write(
                f"""
[general]
debug = false

[codes]
length = 6
timeout = 300
max_attempts = 3
storage_dir = {self.storage_dir}

[notifications]
apprise_urls = {self.notify_url}

[users]
allow_unconfigured_users = false
auth_method = code

{extra}
"""
            )

    def test_correct_code_succeeds_on_password_locked_account(self):
        # The core regression: 2FA must work even though this account
        # cannot authenticate with a Unix password at all.
        self._write_config()
        proc = subprocess.Popen(
            [PAMTESTER, PAM_SERVICE_NAME, TEST_USER, "authenticate"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            code = _wait_for_code(self.storage_dir)
            out, _ = proc.communicate(input=code + "\n", timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()

        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("successfully authenticated", out)
        # No password prompt should ever have appeared -- if it had,
        # pamtester would have consumed the code as the password
        # response and then hung waiting for a second prompt it never
        # received, since we only sent one line of input.
        self.assertNotIn("Password", out)

    def test_wrong_code_is_rejected(self):
        self._write_config()
        returncode, out = _run_pamtester(TEST_USER, ["000000"])
        self.assertNotEqual(returncode, 0)
        self.assertIn("Authentication failure", out)

    def test_bypass_user_grants_access_without_any_prompt(self):
        # Regression test for the PAM_IGNORE -> PAM_SUCCESS fix: with
        # this module as the sole auth line, PAM_IGNORE resolves to an
        # overall deny (nothing else in the stack sets success), so a
        # bypassed user was locked out entirely until this returned
        # PAM_SUCCESS instead.
        self._write_config(extra=f"[bypass]\nusers = {TEST_USER}\n")
        returncode, out = _run_pamtester(TEST_USER, [])
        self.assertEqual(returncode, 0, out)
        self.assertIn("successfully authenticated", out)
        self.assertNotIn("Enter verification code", out)

    def test_unconfigured_user_is_denied(self):
        self._write_config()  # no per-user config, no global apprise_urls override
        with open(self.config_path, "w") as f:
            f.write(
                f"""
[codes]
storage_dir = {self.storage_dir}

[notifications]
apprise_urls =

[users]
allow_unconfigured_users = false
"""
            )
        returncode, out = _run_pamtester(TEST_USER, [])
        self.assertNotEqual(returncode, 0)
        self.assertIn("2FA not configured", out)

    def test_allow_unconfigured_users_grants_access(self):
        with open(self.config_path, "w") as f:
            f.write(
                f"""
[codes]
storage_dir = {self.storage_dir}

[notifications]
apprise_urls =

[users]
allow_unconfigured_users = true
"""
            )
        returncode, out = _run_pamtester(TEST_USER, [])
        self.assertEqual(returncode, 0, out)
        self.assertIn("successfully authenticated", out)

    def test_notification_service_unavailable_denies_access(self):
        # Point at a port nothing listens on (connection refused), so
        # notification delivery fails and the module must deny closed
        # rather than silently let the user through.
        with open(self.config_path, "w") as f:
            f.write(
                f"""
[codes]
storage_dir = {self.storage_dir}

[notifications]
apprise_urls = json://127.0.0.1:1/unreachable

[users]
allow_unconfigured_users = false
"""
            )
        returncode, out = _run_pamtester(TEST_USER, [])
        self.assertNotEqual(returncode, 0)
        self.assertIn(
            "Authentication service cannot retrieve authentication info", out
        )

    def test_cancellation_via_closed_input_denies_access(self):
        self._write_config()
        proc = subprocess.run(
            [PAMTESTER, PAM_SERVICE_NAME, TEST_USER, "authenticate"],
            input="",  # EOF immediately, before any code is sent
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
