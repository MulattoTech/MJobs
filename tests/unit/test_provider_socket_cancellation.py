"""Cancellation must wake a blocked socket read, including on Linux runners."""
from concurrent.futures import ThreadPoolExecutor
import socket
import threading
from types import SimpleNamespace
import unittest

from services.ai.providers.ollama import _shutdown_response_socket


class ProviderSocketCancellationTests(unittest.TestCase):
    def test_shutdown_notifies_peer_of_cancelled_request(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        client.settimeout(5)
        server.settimeout(1)
        ready = threading.Event()
        class NetworkStream:
            def get_extra_info(self, name):
                return client if name == 'socket' else None
        response = SimpleNamespace(extensions={'network_stream': NetworkStream()})
        def read():
            ready.set()
            try:
                return server.recv(1)
            except OSError:
                return b''
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(read)
            self.assertTrue(ready.wait(1))
            _shutdown_response_socket(response)
            self.assertEqual(future.result(timeout=1), b'')
            client.close()

    def test_custom_transport_without_socket_remains_supported(self):
        _shutdown_response_socket(None)
        _shutdown_response_socket(SimpleNamespace(extensions={}))
        class UnsupportedStream:
            def get_extra_info(self, name):
                raise NotImplementedError('synthetic custom transport')
        _shutdown_response_socket(SimpleNamespace(extensions={'network_stream': UnsupportedStream()}))


if __name__ == '__main__':
    unittest.main()
