"""Real owned loopback TLS/HTTP policy controls; no public requests."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import ssl
import threading
import time
import unittest
import warnings
from unittest.mock import patch

from fastapi.testclient import TestClient
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.monitoring import source_version
from services.company_discovery.monitoring_policy import MonitoringPolicy
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError
from tests.integration.test_company_monitoring_recovery import MonitoringFixture
from workers.tasks import execute_operation


class CompanyMonitoringPolicyTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MonitoringFixture()
        self.addCleanup(self.fixture.close)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'employer.example')])
        stamp = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(stamp-timedelta(minutes=1))
            .not_valid_after(stamp+timedelta(hours=1)).add_extension(
                x509.SubjectAlternativeName([x509.DNSName('employer.example')]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
        certfile = self.fixture.root / 'cert.pem'
        keyfile = self.fixture.root / 'key.pem'
        certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        self.context = ssl.create_default_context()
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_verify_locations(cafile=str(certfile))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.minimum_version = ssl.TLSVersion.TLSv1_2
        server_context.load_cert_chain(str(certfile), str(keyfile))
        self.starts = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.release = threading.Event()
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                with fixture.lock:
                    fixture.starts.append((self.path, time.monotonic()))
                if self.path == '/stall-header':
                    fixture.release.wait(3)
                    return
                if self.path == '/stall-body':
                    self.send_response(200)
                    self.send_header('Content-Length', '20')
                    self.end_headers()
                    self.wfile.write(b'x')
                    self.wfile.flush()
                    fixture.release.wait(3)
                    return
                if self.path.startswith('/jobs/'):
                    with fixture.lock:
                        fixture.active += 1
                        fixture.peak = max(fixture.peak, fixture.active)
                    try:
                        fixture.release.wait(1.3)
                    finally:
                        with fixture.lock:
                            fixture.active -= 1
                body = fixture.fixture.pages[self.path].encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(body)
        class Server(ThreadingHTTPServer):
            daemon_threads = False
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.socket = server_context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def transport(self):
        # Only resolution/trust anchor are fixture seams. HTTPS origin checks,
        # actual sockets/TLS/HTTP parsing, rate, timeout and ownership stay real.
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(PublicCompanyClient, '_resolve',
            return_value=[(socket.AF_INET, self.server.server_address)]))
        stack.enter_context(patch('services.company_discovery.public_client.ssl.create_default_context',
                                 return_value=self.context))
        return stack

    def test_real_tls_worker_policy_parallel_sampling_receipt_and_fresh_readback(self):
        rows = [dict(id=str(index), title='Python Engineer', content_text='Python SQL',
                     url=f'https://employer.example/jobs/{index}') for index in range(8)]
        self.fixture.pages['/jobs.json'] = json.dumps(dict(version='https://jsonfeed.org/version/1.1', items=rows))
        for index in range(8):
            self.fixture.pages[f'/jobs/{index}'] = '<html>No structured identity</html>'
        def app(store):
            return create_app(store, 'http://127.0.0.1:1', self.fixture.root,
                              'redis://127.0.0.1:1/0', operation_mode='PERSONAL')
        with TestClient(app(self.fixture.store)) as api:
            response = api.put(f"/company-sources/{self.fixture.source['id']}/monitoring", json=dict(
                source_version=source_version(self.fixture.source), revision=0, poll_interval=300,
                request_timeout=2, concurrency=2, jitter_seconds=17))
            self.assertEqual(response.status_code, 200, response.text)
            monitor = response.json()
            response = api.post(f"/company-sources/{self.fixture.source['id']}/refresh", json={'profile_id':'fixture'})
            self.assertEqual(response.status_code, 202, response.text)
            operation_id = response.json()['operation_id']
        with self.transport(), patch.dict(os.environ, self.fixture.env):
            result = execute_operation(operation_id)
            self.assertEqual(result['status'], 'COMPLETED', result)
            count = len(self.starts)
            self.assertEqual(execute_operation(operation_id)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
            self.assertEqual(len(self.starts), count, 'receipt replay must not fetch')
        self.assertEqual(count, 8, 'robots, career, feed, exactly five samples')
        self.assertEqual(self.peak, 2)
        self.assertEqual(self.active, 0)
        self.assertTrue(all(b[1] - a[1] >= .90 for a,b in zip(self.starts, self.starts[1:])))
        self.assertFalse(any(thread.name.startswith('mjobs-monitor-link') for thread in threading.enumerate()))
        reopened = Store(self.fixture.store.url)
        try:
            with TestClient(app(reopened)) as api:
                readback = api.get(f'/operations/{operation_id}').json()
                self.assertEqual(readback['state'], 'COMPLETED')
                source = next(row for row in api.get('/company-sources/view').json()['company_sources']
                              if row['id'] == self.fixture.source['id'])
                self.assertEqual(source['monitoring']['status'], 'HEALTHY')
                self.assertEqual(source['monitoring']['concurrency'], 2)
                self.assertNotIn('request_not_before', source['monitoring'])
            with reopened.engine.connect() as connection:
                saved = connection.execute(select(m.company_source_monitoring)).mappings().one()
                self.assertEqual(saved['completed_operation_id'], operation_id)
                self.assertEqual(saved['status'], 'HEALTHY')
                self.assertEqual(saved['revision'], monitor['revision'])
                self.assertEqual(saved['concurrency'], 2)
                self.assertEqual(connection.execute(select(m.operations.c.state).where(m.operations.c.id == operation_id)).scalar_one(), 'COMPLETED')
                self.assertIsNotNone(saved['request_not_before'])
            self.assertEqual(reopened.count('canonical_jobs'), 8)
            self.assertEqual(reopened.count('source_quality_observations'), 1)
        finally:
            reopened.engine.dispose()

    def test_actual_tls_header_and_body_stalls_observe_two_second_request_budget(self):
        for path in ('/stall-header', '/stall-body'):
            with self.subTest(path=path):
                client = PublicCompanyClient('https://employer.example', policy=MonitoringPolicy(request_timeout=2))
                try:
                    with self.transport():
                        start = time.monotonic()
                        with self.assertRaisesRegex(PublicFetchError, 'FETCH_DEADLINE'):
                            client._request_once('https://employer.example' + path, time.monotonic() + 10)
                        self.assertLess(time.monotonic() - start, 3)
                    self.assertEqual(client._sockets, set())
                finally:
                    client.close()

    def test_cancel_during_real_tls_handshake_owns_detached_tls_socket(self):
        from workers.operations import OperationCancelled
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(3)
        entered, cancelled = threading.Event(), threading.Event()
        peers, errors = [], []
        def serve():
            try:
                peer, _ = listener.accept()
                peers.append(peer)
                with peer:
                    peer.settimeout(3)
                    if peer.recv(4096):
                        entered.set()
                    peer.recv(4096)  # no TLS response; wait for owned client closure
            except OSError:
                pass
        def check():
            if cancelled.is_set():
                raise OperationCancelled()
        client = PublicCompanyClient('https://employer.example', check=check,
                                     policy=MonitoringPolicy(request_timeout=2))
        def request():
            try:
                client._request_once('https://employer.example/jobs', time.monotonic() + 10)
            except BaseException as error:
                errors.append(error)
        server_thread = threading.Thread(target=serve)
        server_thread.start()
        thread = threading.Thread(target=request)
        try:
            with patch.object(client, '_resolve', return_value=[(socket.AF_INET, listener.getsockname())]), patch(
                    'services.company_discovery.public_client.ssl.create_default_context', return_value=self.context):
                thread.start()
                self.assertTrue(entered.wait(3), 'actual TLS ClientHello must precede cancellation')
                start = time.monotonic()
                cancelled.set()
                thread.join(.5)
                self.assertFalse(thread.is_alive(), 'handshake must close the owned detached SSLSocket')
                self.assertLess(time.monotonic()-start, .5)
                self.assertIsInstance(errors[0], OperationCancelled)
        finally:
            cancelled.set()
            for peer in peers:
                try:
                    peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                peer.close()
            client.close()
            thread.join(3)
            listener.close()
            server_thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(client._sockets, set())

    def test_both_client_paths_enforce_explicit_tls12_floor(self):
        for policy in (None, MonitoringPolicy(request_timeout=2)):
            with self.subTest(policy=policy):
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', DeprecationWarning)
                    self.context.minimum_version = ssl.TLSVersion.TLSv1
                client = PublicCompanyClient('https://employer.example', policy=policy)
                try:
                    with self.transport():
                        response = client._request_once('https://employer.example/robots.txt', time.monotonic()+5)
                    self.assertEqual(response.status_code, 200)
                    self.assertGreaterEqual(self.context.minimum_version, ssl.TLSVersion.TLSv1_2)
                finally:
                    client.close()
