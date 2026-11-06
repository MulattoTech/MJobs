"""Response-close failures on real owned TLS connections; no public requests."""
import http.client
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch

from services.company_discovery.monitoring_policy import MonitoringPolicy
from services.company_discovery.public_client import (
    PublicCompanyClient,
    PublicFetchError,
)
from tests.integration import test_public_company_tls_policy


class PublicCompanyResponseCleanupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.certificates = test_public_company_tls_policy.PublicCompanyTlsPolicyTests("runTest")
        self.addCleanup(self.certificates.doCleanups)
        self.certificates.setUp()

    def request_with_failing_response_close(self, *, reject_headers):
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.minimum_version = ssl.TLSVersion.TLSv1_2
        server_context.load_cert_chain(
            str(self.certificates.certificate), str(self.certificates.key),
        )
        client_context = ssl.create_default_context()
        client_context.load_verify_locations(cafile=str(self.certificates.certificate))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        address = listener.getsockname()
        observed = {}

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    with server_context.wrap_socket(connection, server_side=True) as tls:
                        observed["request"] = tls.recv(4096)
                        encoding = b"Content-Encoding: gzip\r\n" if reject_headers else b""
                        tls.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                            + encoding + b"Connection: close\r\n\r\nOK"
                        )
            except (OSError, ssl.SSLError) as error:
                observed["server_error"] = type(error).__name__
            finally:
                listener.close()

        thread = threading.Thread(target=serve, name="owned-response-cleanup-server")
        thread.start()
        client = PublicCompanyClient(
            "https://fixture.example.com/jobs", policy=MonitoringPolicy(request_timeout=2),
        )
        tracked, closed_responses = [], []
        track = client._track
        close = http.client.HTTPResponse.close

        def observe_socket(sock):
            tracked.append(sock)
            return track(sock)

        def fail_response_close(response):
            # Close the real response file, then model its OS-level close error.
            close(response)
            closed_responses.append(response)
            raise OSError("synthetic-private-response-close-detail")

        try:
            with patch.object(client, "_resolve", return_value=[(socket.AF_INET, address)]), \
                    patch.object(client, "_track", side_effect=observe_socket), \
                    patch("services.company_discovery.public_client.ssl.create_default_context",
                          return_value=client_context), \
                    patch("services.company_discovery.public_client.http.client.HTTPResponse.close",
                          new=fail_response_close):
                if reject_headers:
                    with self.assertRaises(PublicFetchError) as caught:
                        client._request_once(client.website, time.monotonic() + 5)
                    self.assertEqual(caught.exception.code, "CONTENT_ENCODING_UNSUPPORTED")
                    self.assertEqual(str(caught.exception), "CONTENT_ENCODING_UNSUPPORTED")
                else:
                    response = client._request_once(client.website, time.monotonic() + 5)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, b"OK")
                self.assertEqual(len(tracked), 2)
                self.assertEqual(len(closed_responses), 1)
                # Assert real descriptors before the client's final safety cleanup.
                self.assertTrue(all(sock.fileno() == -1 for sock in tracked))
                self.assertEqual(client._sockets, set())
        finally:
            client.close()
            listener.close()
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertTrue(observed.get("request", b"").startswith(b"GET /jobs HTTP/1.1"))
        self.assertNotIn("server_error", observed)

    def test_successful_tls_response_survives_close_error_and_releases_descriptors(self):
        self.request_with_failing_response_close(reject_headers=False)

    def test_policy_refusal_survives_close_error_and_releases_descriptors(self):
        self.request_with_failing_response_close(reject_headers=True)


if __name__ == "__main__":
    unittest.main()
