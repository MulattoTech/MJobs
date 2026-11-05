"""Discarded TCP socket cleanup must preserve primary request outcomes."""
import errno
import socket
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import httpcore

from services.discovery.sources import http_transport
from workers.operations import OperationCancelled, OperationControlUnavailable


class SourceTcpCleanupTests(unittest.TestCase):
    def connection(self, status=errno.EINPROGRESS, close_error=None):
        connection = Mock(spec=["setblocking", "setsockopt", "connect_ex",
                                "getsockopt", "close"])
        connection.connect_ex.return_value = status
        connection.getsockopt.return_value = 0
        connection.close.side_effect = close_error
        return connection

    @contextmanager
    def sockets(self, connections):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                      ("127.0.0.1", 40000 + index))
                     for index in range(len(connections))]
        with patch.object(http_transport, "_resolve", return_value=addresses), patch.object(
                http_transport.socket, "socket", side_effect=connections) as factory:
            yield factory

    def test_pending_cleanup_preserves_stop_and_attempts_every_socket(self):
        for primary in (OperationCancelled(), OperationControlUnavailable(),
                        httpcore.ConnectTimeout("synthetic deadline"),
                        KeyboardInterrupt("synthetic stop")):
            with self.subTest(primary=type(primary).__name__):
                connections = [self.connection(close_error=OSError("synthetic close")),
                               self.connection(close_error=OSError("second close"))]
                with self.sockets(connections) as factory:
                    def check(factory=factory, connections=connections, primary=primary):
                        if factory.call_count == len(connections):
                            raise primary

                    with self.assertRaises(type(primary)) as caught:
                        http_transport._Backend(check).connect_tcp("fixture.example", 443, timeout=1)
                self.assertIs(caught.exception, primary)
                for connection in connections:
                    connection.close.assert_called_once_with()

    def test_immediate_failure_keeps_the_next_address_available(self):
        rejected = self.connection(errno.ECONNREFUSED, OSError("synthetic close"))
        winner = self.connection(0)
        with self.sockets([rejected, winner]):
            stream = http_transport._Backend(lambda: None).connect_tcp(
                "fixture.example", 443, timeout=1)
        self.assertIs(stream.connection, winner)
        rejected.close.assert_called_once_with()
        winner.close.assert_not_called()
        stream.close()
        winner.close.assert_called_once_with()

    def test_initial_failure_cleanup_does_not_skip_control_check(self):
        primary = OperationCancelled()
        rejected = self.connection(errno.ECONNREFUSED, OSError("synthetic close"))

        def check():
            if rejected.close.call_count:
                raise primary

        with self.sockets([rejected]), self.assertRaises(OperationCancelled) as caught:
            http_transport._Backend(check).connect_tcp("fixture.example", 443, timeout=1)
        self.assertIs(caught.exception, primary)
        rejected.close.assert_called_once_with()

    def test_async_failure_keeps_fixed_connect_error_and_original_cause(self):
        rejected = self.connection(close_error=OSError("synthetic private cleanup detail"))
        rejected.getsockopt.return_value = errno.ECONNREFUSED
        with (
            self.sockets([rejected]),
            patch.object(http_transport.select, "select", return_value=([], [rejected], [])),
            self.assertRaises(httpcore.ConnectError) as caught,
        ):
            http_transport._Backend(lambda: None).connect_tcp(
                "fixture.example", 443, timeout=1)
        self.assertEqual(str(caught.exception), "All bounded source connection attempts failed")
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(caught.exception.__cause__.errno, errno.ECONNREFUSED)
        rejected.close.assert_called_once_with()

    def test_winner_survives_all_losing_socket_cleanup_errors(self):
        losing = [self.connection(close_error=OSError("first close")),
                  self.connection(close_error=OSError("second close"))]
        winner = self.connection(0)
        with self.sockets([*losing, winner]):
            stream = http_transport._Backend(lambda: None).connect_tcp(
                "fixture.example", 443, timeout=1)
        self.assertIs(stream.connection, winner)
        for connection in losing:
            connection.close.assert_called_once_with()
        winner.close.assert_not_called()
        stream.close()
        winner.close.assert_called_once_with()

    def test_unexpected_cleanup_failure_remains_visible(self):
        primary = OperationCancelled()
        cleanup = RuntimeError("synthetic unexpected close failure")
        connection = self.connection(close_error=cleanup)
        with self.sockets([connection]) as factory:
            def check():
                if factory.call_count:
                    raise primary

            with self.assertRaises(RuntimeError) as caught:
                http_transport._Backend(check).connect_tcp("fixture.example", 443, timeout=1)
        self.assertIs(caught.exception, cleanup)
        connection.close.assert_called_once_with()

    def test_explicit_owned_stream_close_still_reports_its_error(self):
        cleanup = OSError("synthetic explicit close failure")
        winner = self.connection(0, cleanup)
        with self.sockets([winner]):
            stream = http_transport._Backend(lambda: None).connect_tcp(
                "fixture.example", 443, timeout=1)
        winner.close.assert_not_called()
        with self.assertRaises(OSError) as caught:
            stream.close()
        self.assertIs(caught.exception, cleanup)
        winner.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
