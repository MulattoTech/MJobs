"""Cancellation closes a stalled request, rather than waiting for its read timeout."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import unittest

from services.ai.providers import AIProviderError, AzureOpenAIProvider, OpenAIProvider
from services.ai.providers.openai_compatible import OpenAICompatibleProvider
from services.ai.providers.local_endpoint import LocalEndpointProvider


class RemoteProviderCancellationTests(unittest.TestCase):
    def test_cancel_interrupts_stalled_body_and_closes_peer(self):
        for provider_type in (OpenAICompatibleProvider, OpenAIProvider,
                              AzureOpenAIProvider, LocalEndpointProvider):
            with self.subTest(provider=provider_type.name):
                started, closed, finish = (threading.Event() for _ in range(3))
                cancelled = threading.Event()

                class Handler(BaseHTTPRequestHandler):
                    protocol_version = 'HTTP/1.1'

                    def do_POST(self):
                        self.rfile.read(int(self.headers['Content-Length']))
                        self.send_response(200)
                        self.send_header('Content-Length', '1000')
                        self.end_headers()
                        self.wfile.write(b'{')
                        self.wfile.flush()
                        started.set()
                        self.connection.settimeout(3)
                        try:
                            if self.connection.recv(1) == b'':
                                closed.set()
                        except (ConnectionResetError, BrokenPipeError):
                            closed.set()
                        except socket.timeout:
                            pass
                        finish.wait(3)

                    def log_message(self, *_args):
                        pass

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                server.daemon_threads = True
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                provider = provider_type(base_url=f'http://127.0.0.1:{server.server_port}',
                                         api_key='synthetic-key')
                try:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        work = pool.submit(provider.generate, task='test', model='fixture',
                                           prompt='synthetic', timeout_seconds=4,
                                           cancellation_event=cancelled)
                        try:
                            self.assertTrue(started.wait(2))
                            cancelled.set()
                            with self.assertRaises(AIProviderError) as error:
                                work.result(timeout=1)
                            self.assertEqual(error.exception.code, 'AI_PROVIDER_CANCELLED')
                            self.assertTrue(closed.wait(1), 'Provider left its peer connected')
                        finally:
                            finish.set()
                finally:
                    finish.set()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
