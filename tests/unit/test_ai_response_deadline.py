"""A provider's request budget must also bound a continuously arriving body."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import threading
import time
import unittest

from services.ai.providers import (
    AIProviderError, AzureOpenAIProvider, LocalEndpointProvider, OllamaProvider,
    OpenAICompatibleProvider, OpenAIProvider,
)


PROVIDERS = (OllamaProvider, OpenAICompatibleProvider, OpenAIProvider,
             AzureOpenAIProvider, LocalEndpointProvider)


class AIResponseDeadlineTests(unittest.TestCase):
    def request(self, provider_type, *, delay, budget):
        closed, finish = threading.Event(), threading.Event()
        body = json.dumps({'response': 'ok', 'done': True} if provider_type is OllamaProvider
                          else {'choices': [{'message': {'content': 'ok'}}]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    for value in body:
                        # Observe the peer's FIN directly. A successful write
                        # after FIN is permitted and is not proof of a live peer.
                        if select.select([self.connection], [], [], 0)[0]:
                            if self.connection.recv(1) == b'':
                                closed.set()
                                return
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        if finish.wait(delay):
                            return
                except (BrokenPipeError, ConnectionResetError):
                    closed.set()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        provider = provider_type(base_url=f'http://127.0.0.1:{server.server_port}')
        started = time.monotonic()
        try:
            try:
                result = provider.generate(task='test', model='fixture', prompt='synthetic',
                                           timeout_seconds=budget)
            except AIProviderError as error:
                result = error.code
            elapsed = time.monotonic() - started
            disconnected = closed.wait(.4) if result != 'ok' else False
            return result, elapsed, disconnected
        finally:
            finish.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_slow_body_times_out_and_closes_transport_for_every_provider(self):
        for provider_type in PROVIDERS:
            for attempt in range(2):
                with self.subTest(provider=provider_type.name, attempt=attempt):
                    result, elapsed, disconnected = self.request(provider_type, delay=.02, budget=.15)
                    self.assertEqual(result, 'AI_PROVIDER_TIMEOUT')
                    self.assertLess(elapsed, .75, 'Request budget was extended by arriving bytes')
                    self.assertTrue(disconnected, 'Deadline did not close the owned transport')
                    self.assertFalse(any(t.name == 'mjobs-response-deadline'
                                         for t in threading.enumerate()),
                                     'Completed request left a deadline watcher running')

    def test_complete_body_within_budget_still_succeeds(self):
        for provider_type in PROVIDERS:
            with self.subTest(provider=provider_type.name):
                result, _elapsed, _closed = self.request(provider_type, delay=0, budget=1)
                self.assertEqual(result, 'ok')
                self.assertFalse(any(t.name == 'mjobs-response-deadline'
                                     for t in threading.enumerate()),
                                 'Successful request left a deadline watcher running')
