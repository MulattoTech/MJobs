"""Actual public-fetch TLS handshakes; no public traffic or retained keys."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
import warnings
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError


class PublicCompanyTlsPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-synthetic-tls-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.certificate = root / "certificate.pem"
        self.key = root / "key.pem"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.example.com")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(minutes=1))
                       .not_valid_after(now + timedelta(hours=1))
                       .add_extension(x509.SubjectAlternativeName([
                           x509.DNSName("fixture.example.com")]), critical=False)
                       .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                       .sign(key, hashes.SHA256()))
        self.certificate.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        self.key.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                              serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))

    def handshake(self, version, *, degraded_control=False, trust=True,
                  hostname="fixture.example.com"):
        # Legacy server/control settings intentionally model the claimed vulnerability.
        # The actual production client context keeps all protocol/verification defaults.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.minimum_version = server.maximum_version = version
        server.set_ciphers("ALL:@SECLEVEL=0")
        server.load_cert_chain(str(self.certificate), str(self.key))
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(4)
        address = listener.getsockname()
        observed = {}

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(4)
                    with server.wrap_socket(connection, server_side=True) as tls:
                        observed["protocol"] = tls.version()
                        observed["request"] = tls.recv(4096)
                        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                                    b"Connection: close\r\n\r\nOK")
            except (OSError, ssl.SSLError) as error:
                observed["error_type"] = type(error).__name__
                observed["error_reason"] = getattr(error, "reason", None)
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        original_context = ssl.create_default_context

        def context():
            result = original_context()
            if trust:
                result.load_verify_locations(cafile=str(self.certificate))
            if degraded_control:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    result.minimum_version = version
                result.set_ciphers("ALL:@SECLEVEL=0")
            return result

        client = PublicCompanyClient(f"https://{hostname}/jobs")
        try:
            # Bypass only public-address resolution to reach an isolated loopback server.
            # Socket creation, hostname verification, TLS and HTTP parsing remain real.
            with patch.object(client, "_resolve", return_value=[(socket.AF_INET, address)]), patch(
                    "services.company_discovery.public_client.ssl.create_default_context",
                    side_effect=context):
                try:
                    response = client._request_once(client.website, time.monotonic() + 5)
                except PublicFetchError as error:
                    self.assertEqual(error.code, "NETWORK_FAILED")
                    accepted = False
                else:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, b"OK")
                    accepted = True
        finally:
            client.close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "Controlled TLS server did not terminate")
        if accepted:
            self.assertTrue(observed.get("request", b"").startswith(b"GET /jobs HTTP/1.1"))
        else:
            self.assertNotIn("request", observed, "Rejected TLS peer received an HTTP request")
        return accepted, observed

    def test_obsolete_protocols_rejected_with_working_negative_controls(self):
        for version, protocol in ((ssl.TLSVersion.TLSv1, "TLSv1"),
                                  (ssl.TLSVersion.TLSv1_1, "TLSv1.1")):
            with self.subTest(protocol=protocol):
                accepted, observed = self.handshake(version, degraded_control=True)
                self.assertTrue(accepted, "Legacy endpoint must work before rejection proves policy")
                self.assertEqual(observed["protocol"], protocol)
                accepted, observed = self.handshake(version)
                self.assertFalse(accepted)
                self.assertIn(observed.get("error_reason"),
                              {"UNSUPPORTED_PROTOCOL", "TLSV1_ALERT_PROTOCOL_VERSION"},
                              "Rejection must be a protocol failure, not an unrelated network error")

    def test_tls12_trusted_hostname_succeeds(self):
        accepted, observed = self.handshake(ssl.TLSVersion.TLSv1_2)
        self.assertTrue(accepted)
        self.assertEqual(observed["protocol"], "TLSv1.2")

    def test_untrusted_certificate_rejected_before_http(self):
        self.assertFalse(self.handshake(ssl.TLSVersion.TLSv1_2, trust=False)[0])

    def test_wrong_hostname_rejected_before_http(self):
        self.assertFalse(self.handshake(ssl.TLSVersion.TLSv1_2, hostname="wrong.example.com")[0])
