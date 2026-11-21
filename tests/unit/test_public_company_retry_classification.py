"""Socket failures cross the public boundary without making TLS retryable."""
import http.client
import socket
import ssl
import unittest
from unittest.mock import Mock, patch

from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError


def public_transport_failure(error, fetch=None):
    """Exercise the real public GET with a synthetic TLS failure, without I/O."""
    client = PublicCompanyClient("https://example.test")
    raw = Mock()
    try:
        with patch.object(client, "_resolve", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]), \
                patch.object(client, "_wait"), patch.object(client, "_load_robots"), \
                patch("services.company_discovery.public_client.socket.socket", return_value=raw), \
                patch("services.company_discovery.public_client.ssl.create_default_context") as context:
            context.return_value.wrap_socket.side_effect = error
            try:
                if fetch is None:
                    client.get("https://example.test/jobs")
                else:
                    fetch(client)
            except PublicFetchError as failure:
                raw.close.assert_called()
                assert not client._sockets
                return failure
            raise AssertionError("Expected public transport failure")
    finally:
        client.close()


class PublicCompanyRetryClassificationTests(unittest.TestCase):
    def test_known_transient_socket_errors_are_distinct(self):
        for error in (TimeoutError(), ConnectionResetError(), ConnectionRefusedError(),
                      ConnectionAbortedError(), BrokenPipeError()):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(public_transport_failure(error).code, "TRANSPORT_TRANSIENT")

    def test_tls_protocol_and_unknown_errors_remain_conservative(self):
        certificate = ssl.SSLCertVerificationError(1, "private certificate diagnostic")
        self.assertIsInstance(certificate, OSError)
        for error in (certificate, ssl.SSLError(1, "private TLS diagnostic"),
                      ssl.SSLEOFError(), OSError("unknown"), PermissionError(),
                      http.client.BadStatusLine("private protocol diagnostic"),
                      http.client.RemoteDisconnected()):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(str(public_transport_failure(error)), "NETWORK_FAILED")
