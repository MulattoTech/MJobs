"""Cancellation must interrupt real TLS negotiation, before response ownership."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from services.ai.providers import AIProviderError
from services.ai.providers.transport import request_guard
from tests.unit.test_ai_header_deadline import HeaderServer
from tests.unit.test_ai_response_deadline import PROVIDERS


HTTPS_PROVIDERS = tuple(p for p in PROVIDERS if p.name in {
    'openai-compatible', 'openai', 'azure-openai'})


@contextmanager
def tls_contexts():
    """Trust a short-lived synthetic certificate only in the fixture client."""
    with tempfile.TemporaryDirectory(prefix='mjobs-handshake-tls-') as directory:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic loopback')])
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
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(str(cert_path), str(key_path))
        yield server, ssl.create_default_context(cafile=str(cert_path))


@contextmanager
def recorded_duplicates():
    original, duplicates = socket.socket.dup, []
    def duplicate(connection):
        result = original(connection)
        duplicates.append(result)
        return result
    with patch.object(socket.socket, 'dup', duplicate):
        yield duplicates


class TLSHandshakeTests(unittest.TestCase):
    def test_stalled_handshake_is_interruptible(self):
        for provider_type in HTTPS_PROVIDERS:
            for cancel in (True, False):
                with self.subTest(provider=provider_type.name, cancel=cancel), recorded_duplicates() as duplicates:
                    self.check_handshake(provider_type, cancel)
                    self.assertEqual(len(duplicates), 1)
                    self.assertEqual(duplicates[0].fileno(), -1, 'Duplicate socket leaked')

    def test_trusted_tls_succeeds_and_default_trust_rejects_certificate(self):
        with tls_contexts() as (server_context, client_context):
            for provider_type in HTTPS_PROVIDERS:
                for trusted in (True, False):
                    with self.subTest(provider=provider_type.name, trusted=trusted), recorded_duplicates() as duplicates:
                        with HeaderServer(provider_type, slow=False, tls_context=server_context,
                                          client_context=client_context if trusted else None) as server:
                            result = server.generate(budget=2)
                            self.assertEqual(result, 'ok' if trusted else 'AI_PROVIDER_UNAVAILABLE')
                            self.assertEqual(server.calls, 1 if trusted else 0)
                            self.assertTrue(server.client.is_closed)
                        self.assertEqual(len(duplicates), 1)
                        self.assertEqual(duplicates[0].fileno(), -1)
                        self.assertFalse(any(t.name == 'mjobs-request-deadline' for t in threading.enumerate()))

    def test_duplicate_failure_closes_tcp_stream_before_pool_ownership(self):
        for provider_type in HTTPS_PROVIDERS:
            sockets = []
            def cannot_duplicate(connection):
                sockets.append(connection)
                raise OSError('synthetic handle exhaustion')
            with self.subTest(provider=provider_type.name), HeaderServer(provider_type, slow=False) as server:
                with patch.object(socket.socket, 'dup', cannot_duplicate):
                    self.assertEqual(server.generate(budget=2), 'AI_PROVIDER_UNAVAILABLE')
                self.assertEqual(server.calls, 0, 'Sent a request without its interruption handle')
                self.assertEqual(len(sockets), 1)
                self.assertEqual(sockets[0].fileno(), -1, 'Unpooled stream leaked')
                self.assertTrue(server.client.is_closed)
                self.assertFalse(any(t.name == 'mjobs-request-deadline' for t in threading.enumerate()))

    def test_custom_trace_without_socket_metadata_preserves_generation(self):
        class UnsupportedMetadata:
            def __init__(self, error_type):
                self.error_type = error_type
            def get_extra_info(self, _name):
                raise self.error_type('synthetic unsupported metadata')
        for provider_type in HTTPS_PROVIDERS:
            for stream in (object(), *(UnsupportedMetadata(error) for error in (
                    AttributeError, OSError, TypeError, ValueError, NotImplementedError))):
                def handler(request):
                    request.extensions['trace']('connection.connect_tcp.complete', {'return_value': stream})
                    return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
                client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
                with self.subTest(provider=provider_type.name, stream=type(stream).__name__):
                    provider = provider_type(base_url='https://synthetic.example.test',
                                             client_factory=lambda **_kwargs: client)
                    self.assertEqual(provider.generate(task='test', model='fixture', prompt='synthetic',
                                                       timeout_seconds=2), 'ok')
                    self.assertTrue(client.is_closed)

    def test_exception_releases_duplicate_without_shutting_down_original_socket(self):
        class Stream:
            def __init__(self, connection):
                self.connection = connection
            def get_extra_info(self, _name):
                return self.connection
        class Client:
            def close(self):
                pass
        left, right = socket.socketpair()
        try:
            with recorded_duplicates() as duplicates:
                with self.assertRaisesRegex(ValueError, 'synthetic error'):
                    with request_guard(Client(), deadline=time.monotonic() + 2) as guard:
                        guard.trace('connection.connect_tcp.complete', {'return_value': Stream(left)})
                        raise ValueError('synthetic error')
            self.assertEqual(len(duplicates), 1)
            self.assertEqual(duplicates[0].fileno(), -1)
            right.settimeout(.5)
            left.sendall(b'usable')
            self.assertEqual(right.recv(6), b'usable')
        finally:
            left.close()
            right.close()

    def check_handshake(self, provider_type, cancel):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(3)
        started, closed, finish = (threading.Event() for _ in range(3))
        errors = []

        def peer():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(.05)
                    # Consume ClientHello but deliberately never answer it.
                    while not finish.is_set():
                        try:
                            data = connection.recv(8192)
                        except socket.timeout:
                            continue
                        if not data:
                            closed.set()
                            return
                        started.set()
            except (ConnectionResetError, ConnectionAbortedError):
                closed.set()
            except Exception as error:
                errors.append(type(error).__name__)

        thread = threading.Thread(target=peer)
        thread.start()
        # Default TLS verification stays enabled. The peer never sends a cert.
        client = httpx.Client(trust_env=False, timeout=1.5)
        provider = provider_type(base_url=f'https://127.0.0.1:{listener.getsockname()[1]}',
                                 client_factory=lambda **_kwargs: client)
        event = threading.Event()

        def request():
            try:
                return provider.generate(task='test', model='fixture', prompt='synthetic',
                    timeout_seconds=2 if cancel else .3,
                    cancellation_event=event if cancel else None)
            except AIProviderError as error:
                return error.code

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                before = time.monotonic()
                future = pool.submit(request)
                self.assertTrue(started.wait(1), 'TLS handshake was not exercised')
                if cancel:
                    before = time.monotonic()
                    event.set()
                result = future.result(timeout=3)
                self.assertEqual(result, 'AI_PROVIDER_CANCELLED' if cancel else 'AI_PROVIDER_TIMEOUT')
                self.assertLess(time.monotonic() - before, .9, 'Waited for phase timeout')
            self.assertTrue(closed.wait(.4), 'Peer socket remained open')
            self.assertEqual(errors, [])
            self.assertTrue(client.is_closed)
            self.assertFalse(any(t.name == 'mjobs-request-deadline' for t in threading.enumerate()))
        finally:
            finish.set()
            thread.join(3)
            listener.close()
            client.close()
            self.assertFalse(thread.is_alive(), 'Fixture peer leaked')
