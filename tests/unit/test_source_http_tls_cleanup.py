"""TLS setup cleanup must preserve the failure that stopped the request."""
import ssl
import unittest
from unittest.mock import Mock

import httpcore

from services.discovery.sources.http_transport import _Stream
from workers.operations import OperationCancelled, OperationControlUnavailable


class SourceTlsCleanupTests(unittest.TestCase):
    def transport(self, close_error=None):
        raw = Mock(spec=["close"])
        tls = Mock(spec=["close", "setblocking", "do_handshake"])
        raw.close.side_effect = close_error
        tls.close.side_effect = close_error
        context = Mock()
        context.wrap_socket.return_value = tls
        check = Mock()
        return _Stream(raw, check), context, check, raw, tls

    def test_initial_control_failure_survives_expected_socket_close_error(self):
        for primary in (
            OperationCancelled(), OperationControlUnavailable(),
            httpcore.ConnectTimeout("synthetic deadline"), KeyboardInterrupt("synthetic stop"),
        ):
            with self.subTest(primary=type(primary).__name__):
                stream, context, check, raw, tls = self.transport(OSError("synthetic close"))
                check.side_effect = primary
                with self.assertRaises(type(primary)) as caught:
                    stream.start_tls(context, server_hostname="fixture.example", timeout=1)
                self.assertIs(caught.exception, primary)
                raw.close.assert_called_once_with()
                tls.close.assert_not_called()
                context.wrap_socket.assert_not_called()

    def test_wrap_failure_survives_expected_socket_close_error(self):
        primary = ssl.SSLError("synthetic wrapping failure")
        stream, context, _check, raw, tls = self.transport(OSError("synthetic close"))
        context.wrap_socket.side_effect = primary
        with self.assertRaises(ssl.SSLError) as caught:
            stream.start_tls(context, server_hostname="fixture.example", timeout=1)
        self.assertIs(caught.exception, primary)
        raw.close.assert_called_once_with()
        tls.close.assert_not_called()

    def test_wrapped_setup_failure_closes_tls_and_keeps_original_error(self):
        primary = OSError("synthetic nonblocking setup failure")
        stream, context, _check, raw, tls = self.transport(OSError("synthetic close"))
        tls.setblocking.side_effect = primary
        with self.assertRaises(OSError) as caught:
            stream.start_tls(context, server_hostname="fixture.example", timeout=1)
        self.assertIs(caught.exception, primary)
        tls.close.assert_called_once_with()
        raw.close.assert_not_called()

    def test_handshake_deadline_survives_expected_socket_close_error(self):
        primary = httpcore.ConnectTimeout("synthetic handshake deadline")
        stream, context, _check, raw, tls = self.transport(OSError("synthetic close"))
        tls.do_handshake.side_effect = primary
        with self.assertRaises(httpcore.ConnectTimeout) as caught:
            stream.start_tls(context, server_hostname="fixture.example", timeout=1)
        self.assertIs(caught.exception, primary)
        tls.close.assert_called_once_with()
        raw.close.assert_not_called()

    def test_successful_tls_setup_retains_stream_until_explicit_close(self):
        stream, context, check, raw, tls = self.transport()
        self.assertIs(stream.start_tls(context, server_hostname="fixture.example", timeout=1), stream)
        context.wrap_socket.assert_called_once_with(
            raw, server_hostname="fixture.example", do_handshake_on_connect=False,
        )
        tls.setblocking.assert_called_once_with(False)
        tls.do_handshake.assert_called_once_with()
        raw.close.assert_not_called()
        tls.close.assert_not_called()
        self.assertEqual(check.call_count, 2)
        stream.close()
        tls.close.assert_called_once_with()

    def test_unexpected_cleanup_failure_is_not_silenced(self):
        cleanup = RuntimeError("synthetic unexpected close failure")
        stream, context, check, raw, _tls = self.transport(cleanup)
        check.side_effect = OperationCancelled()
        with self.assertRaises(RuntimeError) as caught:
            stream.start_tls(context, server_hostname="fixture.example", timeout=1)
        self.assertIs(caught.exception, cleanup)
        raw.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
