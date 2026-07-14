#!/usr/bin/env python3
"""Regression tests for approval_server.py's native TLS support."""

import http.client
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from approval_server import ApprovalManager, ApprovalServer

OPENSSL = shutil.which("openssl")


def generate_self_signed_cert(directory: str) -> tuple:
    cert_path = str(Path(directory) / "cert.pem")
    key_path = str(Path(directory) / "key.pem")
    subprocess.run(
        [
            OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            key_path,
            "-out",
            cert_path,
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert_path, key_path


@unittest.skipUnless(OPENSSL, "openssl CLI not available to generate a test cert")
class ApprovalServerTLSTests(unittest.TestCase):
    def setUp(self):
        self.certdir = tempfile.TemporaryDirectory()
        self.cert_path, self.key_path = generate_self_signed_cert(self.certdir.name)
        self.statedir = tempfile.TemporaryDirectory()
        self.manager = ApprovalManager(self.statedir.name)

    def tearDown(self):
        self.certdir.cleanup()
        self.statedir.cleanup()

    def test_server_without_tls_args_serves_plain_http(self):
        server = ApprovalServer("127.0.0.1", 0, self.manager)
        try:
            self.assertFalse(server.tls_enabled)
            self.assertNotIsInstance(server.socket, ssl.SSLSocket)
        finally:
            server.server_close()

    def test_server_with_cert_and_key_wraps_socket_in_tls(self):
        server = ApprovalServer(
            "127.0.0.1",
            0,
            self.manager,
            tls_cert=self.cert_path,
            tls_key=self.key_path,
        )
        try:
            self.assertTrue(server.tls_enabled)
            self.assertIsInstance(server.socket, ssl.SSLSocket)
        finally:
            server.server_close()

    def test_mismatched_cert_and_key_args_are_rejected(self):
        with self.assertRaises(ValueError):
            ApprovalServer(
                "127.0.0.1", 0, self.manager, tls_cert=self.cert_path, tls_key=None
            )
        with self.assertRaises(ValueError):
            ApprovalServer(
                "127.0.0.1", 0, self.manager, tls_cert=None, tls_key=self.key_path
            )

    def test_https_request_completes_a_real_tls_handshake(self):
        server = ApprovalServer(
            "127.0.0.1",
            0,
            self.manager,
            tls_cert=self.cert_path,
            tls_key=self.key_path,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

            conn = http.client.HTTPSConnection(
                "127.0.0.1", server.server_port, context=context, timeout=2
            )
            conn.request("GET", "/health")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
