#!/usr/bin/env python3
"""Regression tests for ApprovalManager's server_url security gate.

Approval links are bearer credentials: whoever observes one in transit
can approve the associated SSH login. create_approval() must refuse to
build a link over plain HTTP for a public host unless the operator has
explicitly opted out.
"""

import tempfile
import unittest

from pam_ssh_2fa import ApprovalManager


class ApprovalUrlSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def _manager(self, server_url, allow_insecure_http=False):
        return ApprovalManager(
            self.tempdir.name,
            server_url=server_url,
            allow_insecure_http=allow_insecure_http,
        )

    def test_https_public_hostname_is_allowed(self):
        manager = self._manager("https://ssh.example.com:9110")
        token, link = manager.create_approval("alice", "203.0.113.9")
        self.assertTrue(link.startswith("https://"))

    def test_http_public_hostname_is_rejected(self):
        manager = self._manager("http://ssh.example.com:9110")
        with self.assertRaises(ValueError):
            manager.create_approval("alice", "203.0.113.9")

    def test_http_public_ip_is_rejected(self):
        manager = self._manager("http://8.8.8.8:9110")
        with self.assertRaises(ValueError):
            manager.create_approval("alice", "203.0.113.9")

    def test_http_loopback_is_allowed(self):
        manager = self._manager("http://127.0.0.1:9110")
        token, link = manager.create_approval("alice", "203.0.113.9")
        self.assertTrue(link.startswith("http://127.0.0.1"))

    def test_http_localhost_is_allowed(self):
        manager = self._manager("http://localhost:9110")
        token, link = manager.create_approval("alice", "203.0.113.9")
        self.assertTrue(link.startswith("http://localhost"))

    def test_http_rfc1918_private_ip_is_allowed(self):
        manager = self._manager("http://10.1.2.3:9110")
        token, link = manager.create_approval("alice", "203.0.113.9")
        self.assertTrue(link.startswith("http://10.1.2.3"))

    def test_http_tailscale_cgnat_ip_is_allowed(self):
        # Tailscale addresses live in 100.64.0.0/10 (RFC 6598 Shared
        # Address Space), which is not globally routable even though
        # Python's ipaddress module doesn't classify it as is_private.
        manager = self._manager("http://100.101.102.103:9110")
        token, link = manager.create_approval("alice", "203.0.113.9")
        self.assertTrue(link.startswith("http://100.101.102.103"))

    def test_http_tailscale_hostname_is_rejected_without_dns(self):
        # A *.ts.net hostname can't be classified as private without a
        # DNS lookup, which this module deliberately never performs in
        # the auth path. Operators should use the tailnet IP literal,
        # `tailscale cert` + native TLS, or the explicit override.
        manager = self._manager("http://myhost.tailnet-name.ts.net:9110")
        with self.assertRaises(ValueError):
            manager.create_approval("alice", "203.0.113.9")

    def test_http_public_host_with_explicit_override_is_allowed(self):
        manager = self._manager(
            "http://ssh.example.com:9110", allow_insecure_http=True
        )
        token, link = manager.create_approval("alice", "203.0.113.9")
        self.assertTrue(link.startswith("http://"))

    def test_unsupported_scheme_is_rejected(self):
        manager = self._manager("ftp://ssh.example.com:9110")
        with self.assertRaises(ValueError):
            manager.create_approval("alice", "203.0.113.9")

    def test_empty_server_url_is_rejected(self):
        manager = self._manager("")
        with self.assertRaises(ValueError):
            manager.create_approval("alice", "203.0.113.9")


if __name__ == "__main__":
    unittest.main()
