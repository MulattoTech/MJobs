"""Owned loopback sockets exercise MockClient before response ownership exists."""
from contextlib import contextmanager
import select
import socket
import threading
import time

import pytest

from services.discovery.pipeline import MockClient


@contextmanager
def fixture_server(*, stalled=False):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(4)
    listener.settimeout(.05)
    origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
    started, peer_closed, finish = (threading.Event() for _ in range(3))
    failures = []

    def serve():
        end = time.monotonic() + 4
        try:
            while not finish.is_set() and time.monotonic() < end:
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(.05)
                    request = bytearray()
                    while b'\r\n\r\n' not in request and not finish.is_set() and time.monotonic() < end:
                        try:
                            chunk = connection.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        request.extend(chunk)
                    started.set()
                    if not stalled:
                        connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
                        continue
                    # No response bytes: the default httpx pool does not yet own
                    # a response that the MockClient watcher can close.
                    while not finish.wait(.01) and time.monotonic() < end:
                        if select.select([connection], [], [], 0)[0]:
                            if connection.recv(1) == b'':
                                peer_closed.set()
                                break
        except BaseException as error:
            failures.append(error)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, name='mock004-owned-loopback')
    thread.start()
    try:
        yield origin, started, peer_closed
    finally:
        finish.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), 'owned fixture thread survived cleanup'
        assert not failures


@pytest.mark.parametrize('trigger', ['cancel', 'deadline'])
def test_pre_response_socket_observes_worker_stop_and_releases_ownership(trigger):
    cancelled = threading.Event()
    deadline = [float('inf')]
    terminal = RuntimeError('OPERATION_CANCELLED' if trigger == 'cancel' else 'OPERATION_DEADLINE_EXCEEDED')

    def check():
        if cancelled.is_set() or time.monotonic() >= deadline[0]:
            raise terminal

    with fixture_server(stalled=True) as (origin, started, peer_closed):
        client = MockClient(origin, check=check)
        errors = []

        def request():
            try:
                client.get(origin + '/jobs')
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=request, name='mock004-owned-request')
        worker.start()
        try:
            assert started.wait(2), 'loopback request never arrived'
            if trigger == 'cancel':
                cancelled.set()
            else:
                deadline[0] = time.monotonic() + .1
            worker.join(timeout=.7)
            assert not worker.is_alive(), 'pre-response socket ignored worker cancellation/deadline'
            assert errors == [terminal], 'worker error classification was lost'
            assert peer_closed.wait(.3), 'owned pre-response socket retained'
        finally:
            # Fixture watchdog closes the peer within four seconds even on RED.
            worker.join(timeout=5)
            client.close()
            assert not worker.is_alive(), 'owned request survived fixture watchdog'
    assert not any(t.name == 'mjobs-fixture-discovery-cancellation' for t in threading.enumerate())
    with fixture_server() as (origin, _started, _closed):
        client = MockClient(origin)
        try:
            assert client.get(origin + '/jobs').json() == {}
        finally:
            client.close()

@contextmanager
def saturated_loopback_listener():
    """Fill only our own listener backlog; never contact an external address."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    address = listener.getsockname()
    fillers = []
    try:
        pending = False
        for _ in range(32):
            filler = socket.socket()
            fillers.append(filler)
            filler.setblocking(False)
            filler.connect_ex(address)
            _, writable, exceptional = select.select([], [filler], [filler], .05)
            if not writable and not exceptional:
                pending = True
                break
        if not pending:
            pytest.skip('owned loopback backlog could not be saturated within32 sockets')
        yield f'http://127.0.0.1:{address[1]}'
    finally:
        listener.close()
        for filler in fillers:
            filler.close()


@pytest.mark.parametrize('trigger', ['cancel', 'deadline'])
def test_pending_real_loopback_connect_observes_worker_stop(trigger, monkeypatch):
    cancelled = threading.Event()
    deadline = [float('inf')]
    terminal = RuntimeError('OPERATION_CANCELLED' if trigger == 'cancel' else 'OPERATION_DEADLINE_EXCEEDED')
    errors = []

    def check():
        if cancelled.is_set() or time.monotonic() >= deadline[0]:
            raise terminal

    with saturated_loopback_listener() as origin:
        sockets = []
        connecting = threading.Event()
        real_socket = socket.socket

        class ObservedSocket(real_socket):
            # Observe real OS calls, not a simulated connect outcome. This patch
            # is confined to this test process and restored by monkeypatch.
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                sockets.append(self)

            def connect(self, address):
                connecting.set()
                return super().connect(address)

            def connect_ex(self, address):
                connecting.set()
                return super().connect_ex(address)

        monkeypatch.setattr(socket, 'socket', ObservedSocket)
        client = MockClient(origin, check=check)

        def request():
            try:
                client.get(origin + '/jobs')
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=request, name='mock004-owned-connect')
        worker.start()
        try:
            # The listener accepts nothing. Its finite backlog is full, so the
            # HTTP connection cannot complete; no response/header fixture exists.
            assert connecting.wait(2), 'no real OS connection attempt observed'
            time.sleep(.15)
            assert worker.is_alive(), 'negative control did not stall connection'
            if trigger == 'cancel':
                cancelled.set()
            else:
                deadline[0] = time.monotonic() + .1
            worker.join(timeout=.7)
            assert not worker.is_alive(), 'pending connect ignored worker cancellation/deadline'
            assert errors == [terminal]
        finally:
            # Default HTTPX connect timeout bounds RED cleanup independently of
            # the worker cancellation being tested. Do not leave an owned thread.
            worker.join(timeout=11)
            client.close()
            assert not worker.is_alive()
            assert sockets and all(s.fileno() == -1 for s in sockets), 'owned connect socket retained'
        monkeypatch.undo()
    with fixture_server() as (origin, _started, _closed):
        follow_on = MockClient(origin)
        try:
            assert follow_on.get(origin + '/jobs').json() == {}
        finally:
            follow_on.close()
    assert not any(t.name == 'mjobs-fixture-discovery-cancellation' for t in threading.enumerate())

@pytest.mark.parametrize('trigger', ['cancel', 'deadline'])
def test_public_fixture_worker_pending_connect_persists_terminal_readback(trigger, tmp_path, monkeypatch):
    """Public admission/direct worker/SQLite reopen; no RQ or external provider."""
    import os
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import create_app
    from packages.persistence.store import Store
    from tests.integration.test_spine import PROFILE
    from workers.tasks import execute_operation

    store = Store(f'sqlite:///{(tmp_path / "fixture.db").as_posix()}')
    store.migrate()
    store.bind_workspace_mode('FIXTURE')
    store.save_profile(dict(PROFILE))
    worker = None
    try:
        with saturated_loopback_listener() as origin:
            connecting = threading.Event()
            sockets = []
            real_socket = socket.socket

            class ObservedSocket(real_socket):
                def connect(self, address):
                    if address[1] == int(origin.rsplit(':', 1)[1]):
                        sockets.append(self)
                        connecting.set()
                    return super().connect(address)

                def connect_ex(self, address):
                    if address[1] == int(origin.rsplit(':', 1)[1]):
                        sockets.append(self)
                        connecting.set()
                    return super().connect_ex(address)

            monkeypatch.setattr(socket, 'socket', ObservedSocket)
            app = create_app(store, origin, tmp_path, 'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            with TestClient(app) as api, patch.dict(os.environ, {
                'MJOBS_DATABASE_URL': store.url, 'MJOBS_MOCK_ORIGIN': origin,
                'MJOBS_OUTPUT_DIR': str(tmp_path), 'MJOBS_OPERATION_MODE': 'FIXTURE',
            }), patch('workers.tasks.timeout', return_value=2 if trigger == 'cancel' else .8):
                admitted = api.post('/discovery', json={'profile_id': 'fixture', 'request_id': 'mock004-' + trigger})
                assert admitted.status_code == 202, admitted.text
                identifier = admitted.json()['operation_id']
                results, errors = [], []

                def execute():
                    try:
                        results.append(execute_operation(identifier))
                    except BaseException as error:
                        errors.append(error)

                worker = threading.Thread(target=execute, name='mock004-public-worker')
                worker.start()
                assert connecting.wait(2), 'public worker did not attempt loopback connection'
                time.sleep(.1)
                assert sockets and not select.select([], [sockets[-1]], [], 0)[1], 'TCP connect not pending'
                if trigger == 'cancel':
                    assert api.post(f'/operations/{identifier}/cancel').status_code == 202
                worker.join(timeout=1.2)
                assert not worker.is_alive(), 'public fixture worker remained blocked in connect'
                assert not errors and len(results) == 1
                saved = api.get(f'/operations/{identifier}').json()
                expected = ('CANCELLED', 'CANCEL_REQUESTED') if trigger == 'cancel' else ('DEAD_LETTERED', 'OPERATION_TIMEOUT')
                assert (saved['state'], saved['error']) == expected
                assert saved['attempt_count'] == 1
                assert all(s.fileno() == -1 for s in sockets)
            reopened = Store(store.url)
            try:
                with TestClient(create_app(reopened, origin, tmp_path, 'redis://127.0.0.1:1/0', operation_mode='FIXTURE')) as api:
                    durable = api.get(f'/operations/{identifier}').json()
                    assert (durable['state'], durable['error'], durable['attempt_count']) == (*expected, 1)
            finally:
                reopened.engine.dispose()
    finally:
        if worker is not None:
            worker.join(timeout=11)
            assert not worker.is_alive(), 'public worker survived finite cleanup watchdog'
        store.engine.dispose()
    assert not any(t.name == 'mjobs-fixture-discovery-cancellation' for t in threading.enumerate())

@pytest.mark.parametrize('origin', ['https://127.0.0.1', 'http://example.invalid', 'http://user:pass@localhost', 'http://localhost/path', 'http://mock'])
def test_fixture_origin_remains_explicitly_allowlisted(origin, monkeypatch):
    monkeypatch.delenv('MJOBS_COMPOSE_MOCK', raising=False)
    with pytest.raises(ValueError, match='explicit local fixture server'):
        MockClient(origin)


def test_compose_mock_origin_still_requires_explicit_gate(monkeypatch):
    monkeypatch.setenv('MJOBS_COMPOSE_MOCK', '1')
    client = MockClient('http://mock:8080')
    try:
        assert client.allowed('http://mock:8080/jobs')
        assert not client.allowed('http://127.0.0.1:8080/jobs')
    finally:
        client.close()


@pytest.mark.parametrize('mode', ['external', 'loop', 'healthy'])
def test_injected_response_controls_preserve_same_origin_redirect_bound(mode, monkeypatch):
    import httpx
    from services.discovery import pipeline
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if mode == 'healthy' and len(calls) == 4:
            return httpx.Response(200, json={'ok': True})
        return httpx.Response(302, headers={'location': 'http://external.invalid/jobs' if mode == 'external' else '/next'})

    monkeypatch.setattr(pipeline, 'SourceTransport', lambda check: httpx.MockTransport(handle))
    client = MockClient('http://127.0.0.1:8080')
    try:
        if mode == 'healthy':
            assert client.get(client.origin + '/jobs').json() == {'ok': True}
        else:
            message = 'Untrusted external redirect blocked' if mode == 'external' else 'Too many source redirects'
            with pytest.raises(ValueError, match=message):
                client.get(client.origin + '/jobs')
        assert len(calls) == (1 if mode == 'external' else 4)
    finally:
        client.close()
