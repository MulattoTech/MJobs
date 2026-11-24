"""A provider's request budget must also bound a continuously arriving body."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx

from services.ai.providers import (
    AIProviderError, AzureOpenAIProvider, LocalEndpointProvider, OllamaProvider,
    OpenAICompatibleProvider, OpenAIProvider,
)
from services.ai.providers import transport


PROVIDERS = (OllamaProvider, OpenAICompatibleProvider, OpenAIProvider,
             AzureOpenAIProvider, LocalEndpointProvider)
# Leave room for loopback scheduling before the body starts. The byte drip
# still runs longer than this total budget and delivers bytes well within it.
BODY_BUDGET_SECONDS = 0.5
BODY_DRIP_SECONDS = 0.02
REQUEST_WATCHER_NAMES = ('mjobs-request-deadline', 'mjobs-request-clock')


class AIResponseDeadlineTests(unittest.TestCase):
    def assert_request_watchers_stopped(self, watchers):
        self.assertCountEqual([thread.name for thread in watchers], REQUEST_WATCHER_NAMES)
        for thread in watchers:
            self.assertIsNotNone(thread.ident, f'{thread.name} was never started')
            self.assertFalse(thread.is_alive(), f'Request watcher {thread.name} is still running')

    def request(self, provider_type, *, delay, budget, pre_header_delay=0):
        closed, finish, headers_received = threading.Event(), threading.Event(), threading.Event()
        payload = ({'response': 'ok', 'done': True} if provider_type is OllamaProvider
                   else {'choices': [{'message': {'content': 'ok'}}]})
        if delay:
            payload['fixture_padding'] = 'x' * 128
        body = json.dumps(payload).encode()
        body_bytes_sent = 0

        def observe_headers(_response):
            headers_received.set()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal body_bytes_sent
                self.rfile.read(int(self.headers['Content-Length']))
                if pre_header_delay and finish.wait(pre_header_delay):
                    return
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    for value in body:
                        # Observe the peer's FIN directly. A successful write
                        # after FIN is permitted and is not proof of a live peer.
                        if select.select([self.connection], [], [], 0)[0]:
                            if self.connection.recv(1) == b'':
                                closed.set()
                                return
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        body_bytes_sent += 1
                        if finish.wait(delay):
                            return
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    closed.set()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        # Keep client/TLS-context construction outside this body-read oracle.
        # Production may correctly exhaust a tiny budget before connecting.
        client = httpx.Client(trust_env=False, timeout=httpx.Timeout(budget),
                              event_hooks={'response': [observe_headers]})
        provider = provider_type(base_url=f'http://127.0.0.1:{server.server_port}',
                                 client_factory=lambda **_kwargs: client)
        caller = threading.current_thread()
        watchers = []

        def observe_transport_thread(*args, **kwargs):
            created = threading.Thread(*args, **kwargs)
            if threading.current_thread() is caller and created.name in REQUEST_WATCHER_NAMES:
                watchers.append(created)
            return created

        transport_threads = SimpleNamespace(
            Thread=observe_transport_thread, Event=threading.Event, Lock=threading.Lock,
        )
        try:
            with patch.object(transport, 'threading', transport_threads):
                started = time.monotonic()
                try:
                    result = provider.generate(task='test', model='fixture', prompt='synthetic',
                                               timeout_seconds=budget)
                except AIProviderError as error:
                    result = error.code
                elapsed = time.monotonic() - started
            self.assert_request_watchers_stopped(watchers)
            self.assertTrue(headers_received.is_set(), 'Client never received response headers')
            disconnected = closed.wait(.4) if result != 'ok' else False
            if delay:
                self.assertGreaterEqual(body_bytes_sent, 2, 'Body oracle never observed continuing writes')
                self.assertLess(body_bytes_sent, len(body), 'Server finished the body before the timeout')
            return result, elapsed, disconnected
        finally:
            client.close()
            finish.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_slow_body_times_out_and_closes_transport_for_every_provider(self):
        for provider_type in PROVIDERS:
            for attempt in range(2):
                with self.subTest(provider=provider_type.name, attempt=attempt):
                    self._assert_slow_body_deadline(provider_type)

    def _assert_slow_body_deadline(self, provider_type, *, pre_header_delay=0):
        result, elapsed, disconnected = self.request(
            provider_type, delay=BODY_DRIP_SECONDS, budget=BODY_BUDGET_SECONDS,
            pre_header_delay=pre_header_delay,
        )
        self.assertEqual(result, 'AI_PROVIDER_TIMEOUT')
        self.assertLess(elapsed, .75, 'Request budget was extended by arriving bytes')
        self.assertTrue(disconnected, 'Deadline did not close the owned transport')

    def test_loopback_header_delay_still_reaches_slow_body_deadline(self):
        # The previous 150 ms fixture budget expired during this phase.
        self._assert_slow_body_deadline(OllamaProvider, pre_header_delay=.25)

    def test_complete_body_within_budget_still_succeeds(self):
        for provider_type in PROVIDERS:
            with self.subTest(provider=provider_type.name):
                result, _elapsed, _closed = self.request(provider_type, delay=0, budget=1)
                self.assertEqual(result, 'ok')

    def test_request_watcher_witness_rejects_only_tracked_live_thread(self):
        release = threading.Event()
        unrelated = threading.Thread(target=release.wait, name='mjobs-request-deadline')
        completed = threading.Thread(target=lambda: None, name='mjobs-request-clock')
        completed.start()
        completed.join(timeout=2)
        self.assertFalse(completed.is_alive())
        unrelated.start()
        try:
            result, _elapsed, _closed = self.request(OllamaProvider, delay=0, budget=1)
            self.assertEqual(result, 'ok')
            self.assertTrue(unrelated.is_alive())
            with self.assertRaisesRegex(AssertionError, 'Request watcher mjobs-request-deadline is still running'):
                self.assert_request_watchers_stopped([unrelated, completed])
        finally:
            release.set()
            unrelated.join(timeout=2)
        self.assertFalse(unrelated.is_alive())
