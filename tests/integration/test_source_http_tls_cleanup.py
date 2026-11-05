"""Real loopback descriptors survive an injected close error during TLS setup."""
import socket
import ssl
import unittest
from unittest.mock import patch

import httpcore

from services.discovery.sources.http_transport import _Stream
from workers.operations import OperationCancelled


class SourceTlsCleanupLoopbackTests(unittest.TestCase):
    def assert_stop_preserved(self, primary, after_wrap):
        context = ssl.create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(2)
            connection = socket.create_connection(listener.getsockname(), timeout=2)
            connection.setblocking(False)
            peer, _address = listener.accept()
        self.addCleanup(peer.close)
        peer.settimeout(2)
        calls = 0

        def check():
            nonlocal calls
            calls += 1
            if calls == (2 if after_wrap else 1):
                raise primary

        stream = _Stream(connection, check)
        close = stream.close
        closed = []

        def close_then_fail():
            # Exercise the actual raw/TLS descriptor close before injecting its
            # expected OS-level error; no public host or external resolver is used.
            close()
            closed.append(stream.connection.fileno())
            raise OSError("synthetic-private-close-detail")

        try:
            with patch.object(stream, "close", side_effect=close_then_fail) as cleanup:
                with self.assertRaises(type(primary)) as caught:
                    stream.start_tls(context, server_hostname="fixture.example", timeout=1)
                self.assertIs(caught.exception, primary)
                cleanup.assert_called_once_with()
                self.assertEqual(closed, [-1])
                self.assertEqual(stream.connection.fileno(), -1)
                self.assertEqual(isinstance(stream.connection, ssl.SSLSocket), after_wrap)
                # Both failures occur before ClientHello, so the peer sees EOF.
                self.assertEqual(peer.recv(1), b"")
        finally:
            stream.connection.close()
            connection.close()

    def test_cancelled_raw_and_wrapped_connections_keep_the_cancellation(self):
        for after_wrap in (False, True):
            with self.subTest(after_wrap=after_wrap):
                self.assert_stop_preserved(OperationCancelled(), after_wrap)

    def test_deadline_raw_and_wrapped_connections_keep_the_deadline(self):
        for after_wrap in (False, True):
            with self.subTest(after_wrap=after_wrap):
                self.assert_stop_preserved(
                    httpcore.ConnectTimeout("synthetic TLS deadline"), after_wrap,
                )


if __name__ == "__main__":
    unittest.main()
