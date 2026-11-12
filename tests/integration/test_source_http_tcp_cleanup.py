"""Owned loopback TCP descriptors close despite expected cleanup errors."""
import socket
import threading
import unittest
from unittest.mock import patch

import httpcore

from services.discovery.sources import http_transport
from tests.integration import test_source_http_socket_budget as fixtures
from workers.operations import OperationCancelled


class CloseErrorSocket:
    def __init__(self, connection, fail_close):
        self.connection = connection
        self.fail_close = fail_close
        self.close_calls = 0
        self.connect_status = None

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def connect_ex(self, address):
        self.connect_status = self.connection.connect_ex(address)
        return self.connect_status

    def close(self):
        self.close_calls += 1
        self.connection.close()
        if self.fail_close:
            raise OSError("synthetic TCP close detail")


class SourceTcpCleanupLoopbackTests(unittest.TestCase):
    def factory(self, connections, fail_close):
        real_socket = socket.socket
        owner_thread = threading.get_ident()

        def create(*args, **kwargs):
            connection = real_socket(*args, **kwargs)
            if threading.get_ident() != owner_thread or kwargs.get("fileno") is not None:
                return connection
            wrapped = CloseErrorSocket(connection, fail_close(len(connections)))
            connections.append(wrapped)
            return wrapped

        return create

    def assert_pending_stop(self, primary):
        connections = []
        with fixtures.tcp_connect_stall() as (_endpoint, _engaged, state):
            addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", state["address"])] * 2

            def check():
                if len(connections) == 2:
                    raise primary

            try:
                with (
                    patch.object(http_transport, "_resolve", return_value=addresses),
                    patch.object(http_transport.socket, "socket",
                                 side_effect=self.factory(connections, lambda _index: True)),
                    self.assertRaises(type(primary)) as caught,
                ):
                    http_transport._Backend(check).connect_tcp(
                        "fixture.example", state["address"][1], timeout=1)
                self.assertIs(caught.exception, primary)
                self.assertEqual(len(connections), 2)
                for connection in connections:
                    self.assertIn(connection.connect_status, http_transport._PENDING)
                    self.assertEqual(connection.close_calls, 1)
                    self.assertEqual(connection.fileno(), -1)
                self.assertEqual(state["requests"], 0)
                self.assertEqual(state["bytes"], 0)
            finally:
                # Assertions above precede this safety-net disposal.
                for connection in connections:
                    connection.connection.close()

    def test_cancellation_closes_every_real_pending_descriptor(self):
        self.assert_pending_stop(OperationCancelled())

    def test_deadline_identity_survives_real_pending_descriptor_cleanup(self):
        self.assert_pending_stop(httpcore.ConnectTimeout("synthetic TCP deadline"))

    def test_losing_socket_close_error_keeps_healthy_http_connection_usable(self):
        connections = []
        stream = None
        with fixtures.tcp_connect_stall() as (_stalled, _unused, stalled), fixtures.tcp_source(
                "positive") as (_healthy, received, healthy):
            addresses = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", stalled["address"]),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", healthy["address"]),
            ]
            try:
                with patch.object(http_transport, "_resolve", return_value=addresses), patch.object(
                        http_transport.socket, "socket",
                        side_effect=self.factory(connections, lambda index: index == 0)):
                    stream = http_transport._Backend(lambda: None).connect_tcp(
                        "fixture.example", healthy["address"][1], timeout=1)
                    self.assertEqual(len(connections), 2)
                    loser, winner = connections
                    self.assertIn(loser.connect_status, http_transport._PENDING)
                    self.assertEqual(loser.close_calls, 1)
                    self.assertEqual(loser.fileno(), -1)
                    self.assertIs(stream.connection, winner)
                    self.assertGreaterEqual(winner.fileno(), 0)
                    self.assertEqual(winner.close_calls, 0)
                    stream.write(
                        b"GET /source HTTP/1.1\r\nHost: fixture.example\r\nConnection: close\r\n\r\n",
                        timeout=1,
                    )
                    response = bytearray()
                    for _ in range(8):
                        part = stream.read(4096, timeout=1)
                        if not part:
                            break
                        response.extend(part)
                    self.assertIn(b"HTTP/1.1 200 OK", response)
                    self.assertIn(b'{"jobs": []}', response)
                    self.assertTrue(received.wait(1))
                    self.assertEqual(healthy["requests"], 1)
                    self.assertEqual(stalled["requests"], 0)
                    stream.close()
                    self.assertEqual(winner.close_calls, 1)
                    self.assertEqual(winner.fileno(), -1)
            finally:
                for connection in connections:
                    connection.connection.close()


if __name__ == "__main__":
    unittest.main()
