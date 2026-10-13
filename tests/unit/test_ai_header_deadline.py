"""Connected provider requests must be interruptible before headers complete."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import threading
import time
import unittest
from unittest.mock import patch
import httpx
import ssl
import tempfile
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from services.ai.providers import AIProviderError, OllamaProvider
from tests.unit.test_ai_response_deadline import PROVIDERS


class HeaderServer:
    def __init__(self, provider_type, *, slow=True, tls_context=None, client_context=None):
        self.started = threading.Event()
        self.closed = threading.Event()
        self.finish = threading.Event()
        self.calls = 0
        owner = self
        body = json.dumps({'response': 'ok', 'done': True} if provider_type is OllamaProvider
                          else {'choices': [{'message': {'content': 'ok'}}]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.calls += 1
                self.rfile.read(int(self.headers['Content-Length']))
                try:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    self.wfile.flush()
                    owner.started.set()
                    for _ in range(30 if slow else 0):
                        if select.select([self.connection], [], [], 0)[0]:
                            if self.connection.recv(1) == b'':
                                owner.closed.set()
                                return
                        self.wfile.write(b'a')
                        self.wfile.flush()
                        if owner.finish.wait(.04):
                            return
                    self.wfile.write(b'\r\nContent-Length: ' + str(len(body)).encode()
                                     + b'\r\n\r\n' + body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLEOFError):
                    owner.closed.set()

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        if tls_context is not None:
            self.server.socket = tls_context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.client = httpx.Client(trust_env=False, verify=client_context or True)
        scheme = 'https' if tls_context is not None else 'http'
        self.provider = provider_type(base_url=f'{scheme}://127.0.0.1:{self.server.server_port}',
                                      client_factory=lambda **_kwargs: self.client)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.finish.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.client.close()

    def generate(self, *, budget, cancellation=None):
        self.client.timeout = httpx.Timeout(budget)
        try:
            return self.provider.generate(task='test', model='fixture', prompt='synthetic',
                                          timeout_seconds=budget, cancellation_event=cancellation)
        except AIProviderError as error:
            return error.code


class AIHeaderDeadlineTests(unittest.TestCase):
    def assert_no_watchdog(self):
        self.assertFalse(any(t.name == 'mjobs-request-deadline' for t in threading.enumerate()))

    def test_trickled_headers_cannot_extend_total_deadline(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.name), HeaderServer(provider) as server:
                start = time.monotonic()
                result = server.generate(budget=.3)
                elapsed = time.monotonic() - start
                self.assertTrue(server.started.is_set(), 'Did not exercise the pre-header boundary')
                self.assertEqual(result, 'AI_PROVIDER_TIMEOUT')
                self.assertLess(elapsed, .8, 'Partial header bytes extended the total request budget')
                self.assertTrue(server.closed.wait(.4), 'Request-owned transport was not interrupted')
                self.assertEqual(server.calls, 1)
                self.assert_no_watchdog()

    def test_cancel_before_headers_interrupts_owned_socket(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.name), HeaderServer(provider) as server:
                event = threading.Event()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(server.generate, budget=4, cancellation=event)
                    self.assertTrue(server.started.wait(2))
                    start = time.monotonic()
                    event.set()
                    self.assertEqual(future.result(timeout=3), 'AI_PROVIDER_CANCELLED')
                    self.assertLess(time.monotonic() - start, .6, 'Cancellation waited for headers')
                self.assertTrue(server.closed.wait(.4))
                self.assertEqual(server.calls, 1)
                self.assert_no_watchdog()

    def test_success_and_independent_request_are_preserved(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.name), HeaderServer(provider) as slow, HeaderServer(provider, slow=False) as fast:
                event = threading.Event()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    blocked = pool.submit(slow.generate, budget=4, cancellation=event)
                    self.assertTrue(slow.started.wait(2))
                    successful = pool.submit(fast.generate, budget=2)
                    event.set()
                    self.assertEqual(blocked.result(timeout=3), 'AI_PROVIDER_CANCELLED')
                    self.assertEqual(successful.result(timeout=3), 'ok')
                self.assert_no_watchdog()

    def test_expired_client_initialization_never_sends_request(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.name):
                ticks, calls = [100.0], []
                client = httpx.Client(transport=httpx.MockTransport(
                    lambda request: calls.append(request) or httpx.Response(200)))
                def factory(**_kwargs):
                    ticks[0] += 2
                    return client
                instance = provider(base_url='http://127.0.0.1:1', client_factory=factory)
                with patch('services.ai.providers.transport.time.monotonic', side_effect=lambda: ticks[0]):
                    with self.assertRaises(AIProviderError) as error:
                        instance.generate(task='test', model='fixture', prompt='synthetic', timeout_seconds=1)
                self.assertEqual(error.exception.code, 'AI_PROVIDER_TIMEOUT')
                self.assertEqual(calls, [])
                self.assertTrue(client.is_closed)
                self.assert_no_watchdog()

    def test_tls_stream_replacement_preserves_header_deadline_and_cancellation(self):
        # A private short-lived CA is trusted only by the synthetic test client.
        # Production certificate/hostname verification remains unchanged.
        with tempfile.TemporaryDirectory(prefix='mjobs-header-tls-') as directory:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'local header fixture')])
            now = datetime.now(timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                    .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address('127.0.0.1'))]), False)
                    .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
                    .sign(key, hashes.SHA256()))
            cert_path, key_path = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
            tls_context.load_cert_chain(str(cert_path), str(key_path))
            client_context = ssl.create_default_context(cafile=str(cert_path))
            # Ollama and Local Endpoint intentionally accept loopback HTTP only.
            for provider in (p for p in PROVIDERS if p.name in {'openai-compatible', 'openai', 'azure-openai'}):
                for cancel in (False, True):
                    with self.subTest(provider=provider.name, cancel=cancel), HeaderServer(
                            provider, tls_context=tls_context, client_context=client_context) as server:
                        event = threading.Event()
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            started = time.monotonic()
                            future = pool.submit(server.generate, budget=4 if cancel else .3,
                                                 cancellation=event if cancel else None)
                            self.assertTrue(server.started.wait(2), 'TLS headers were never reached')
                            if cancel:
                                started = time.monotonic()
                                event.set()
                            self.assertEqual(future.result(timeout=3),
                                             'AI_PROVIDER_CANCELLED' if cancel else 'AI_PROVIDER_TIMEOUT')
                            self.assertLess(time.monotonic() - started, .8)
                        self.assertTrue(server.closed.wait(.4), 'TLS connection remained open')
                        self.assert_no_watchdog()
