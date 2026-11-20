"""Owned loopback sockets exercise MockClient before response ownership exists."""
from collections import deque
from contextlib import ExitStack, contextmanager
from pathlib import Path
import select
import socket
import sys
import threading
import time

import httpx
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


class _PendingConnectClock:
    """Start only the worker's test budget after real pending-connect admission."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._origin = clock()
        self._admitted_at = None

    def admit(self):
        # Admission is one-way: teardown or a repeated call cannot reset expiry.
        if self._admitted_at is None:
            self._admitted_at = self._clock()

    def monotonic(self):
        admitted_at = self._admitted_at
        if admitted_at is None:
            return self._origin
        return self._origin + self._clock() - admitted_at


def test_pending_connect_clock_excludes_preparation_but_cannot_extend_admitted_budget():
    wall = [10.0]
    clock = _PendingConnectClock(lambda: wall[0])
    deadline = clock.monotonic() + .8
    wall[0] += 30
    assert clock.monotonic() == 10
    clock.admit()
    wall[0] += .4
    assert clock.monotonic() == pytest.approx(10.4)
    clock.admit()
    wall[0] += .41
    assert clock.monotonic() > deadline


class _PublicWorkerPhases:
    """Bounded observations of the owned worker, without payloads or SQL values."""

    def __init__(self, *, hold_terminal_commit=False):
        self.started = time.monotonic()
        self.records = deque(maxlen=128)
        self.worker_store = None
        self.http_client = None
        self.watcher = None
        self.sockets = []
        self.operation_id = None
        self.transport_unwound = False
        self.hold_terminal_commit = hold_terminal_commit
        self.terminal_held = threading.Event()
        self.release_terminal = threading.Event()
        self.failure_snapshot = None
        self.worker_error_types = []
        self.worker_result_count = 0

    def record(self, label):
        self.records.append((label, time.monotonic() - self.started,
                             time.thread_time(), threading.current_thread().name))

    def span(self, label, operation):
        def observed(*args, **kwargs):
            self.record(label + '_enter')
            try:
                return operation(*args, **kwargs)
            except BaseException as error:
                self.record(label + '_' + type(error).__name__)
                raise
            finally:
                self.record(label + '_exit')
        return observed

    def budget(self, value):
        self.record('deadline_budget')
        return value

    def observe(self, stack, database_url):
        from unittest.mock import patch
        from packages.persistence.store import Store
        from services.discovery.sources.http_transport import _Backend
        from workers import operations, tasks

        original_init = Store.__init__
        original_read = operations._read_operation_control
        original_get = MockClient.get
        original_http_close = httpx.Client.close

        def observed_store_init(store, url):
            if (url != database_url
                    or threading.current_thread().name != 'mock004-public-worker'):
                return original_init(store, url)
            self.record('worker_store_enter')
            try:
                original_init(store, url)
                self.worker_store = store
                self.observe_engine(stack, store.engine)
            finally:
                self.record('worker_store_exit')

        def observed_read(store, *args, **kwargs):
            if store is not self.worker_store:
                return original_read(store, *args, **kwargs)
            if threading.current_thread().name == 'mjobs-fixture-discovery-cancellation':
                self.watcher = threading.current_thread()
            return self.span('control_read', original_read)(store, *args, **kwargs)

        def observed_get(client, *args, **kwargs):
            self.http_client = client.client
            try:
                return self.span('mock_get', original_get)(client, *args, **kwargs)
            finally:
                self.transport_unwound = True

        def observed_http_close(client):
            if client is self.http_client:
                if threading.current_thread().name == 'mjobs-fixture-discovery-cancellation':
                    self.watcher = threading.current_thread()
                return self.span('http_close', original_http_close)(client)
            return original_http_close(client)

        stack.enter_context(patch.object(Store, '__init__', observed_store_init))
        stack.enter_context(patch.object(operations, '_read_operation_control', observed_read))
        stack.enter_context(patch.object(tasks, 'claim', self.span('claim', tasks.claim)))
        stack.enter_context(patch.object(_Backend, 'connect_tcp',
                                        self.span('backend_connect', _Backend.connect_tcp)))
        stack.enter_context(patch.object(MockClient, 'get', observed_get))
        stack.enter_context(patch.object(MockClient, 'close', self.span('mock_close', MockClient.close)))
        stack.enter_context(patch.object(httpx.Client, 'close', observed_http_close))
        stack.enter_context(patch.object(tasks, 'log_operation_finished',
                                        self.span('log_finished', tasks.log_operation_finished)))

    def observe_engine(self, stack, engine):
        from unittest.mock import patch
        from sqlalchemy import event
        from packages.persistence import models as m

        terminal_commit = [False]
        original_commit = engine.dialect.do_commit

        def sql_label(statement):
            upper = statement.lstrip().upper()
            verb = upper.split(None, 1)[0] if upper else 'EMPTY'
            table = ('operations' if m.operations.name.upper() in upper else
                     'audit' if m.audit.name.upper() in upper else 'other')
            return verb + '_' + table

        def before_sql(_connection, _cursor, statement, *_args):
            if self.transport_unwound:
                label = sql_label(statement)
                self.record('sql_' + label + '_enter')
                if label == 'UPDATE_operations':
                    terminal_commit[0] = True

        def after_sql(_connection, _cursor, statement, *_args):
            if self.transport_unwound:
                self.record('sql_' + sql_label(statement) + '_exit')

        def observed_commit(connection):
            label = 'terminal_commit' if terminal_commit[0] else 'dbapi_commit'
            self.record(label + '_enter')
            try:
                if terminal_commit[0] and self.hold_terminal_commit:
                    self.record('terminal_commit_held')
                    self.terminal_held.set()
                    if not self.release_terminal.wait(5):
                        raise AssertionError('diagnostic terminal release watchdog expired')
                    self.record('terminal_commit_released')
                result = original_commit(connection)
                self.record(label + '_returned')
                terminal_commit[0] = False
                return result
            finally:
                self.record(label + '_exit')

        for name, observer in (('before_cursor_execute', before_sql),
                               ('after_cursor_execute', after_sql)):
            event.listen(engine, name, observer)
            stack.callback(event.remove, engine, name, observer)
        stack.enter_context(patch.object(engine.dialect, 'do_commit', observed_commit))
        stack.enter_context(patch.object(engine, 'dispose', self.span('store_dispose', engine.dispose)))

    def describe_failure(self, worker, *, phase):
        # Fixed call-site phases; exact Thread objects, never frames or locals.
        self.record(phase)
        frames = sys._current_frames()
        stacks = {}
        try:
            for thread in (worker, self.watcher):
                if thread is None:
                    continue
                entries = []
                frame = frames.get(thread.ident) if thread.is_alive() else None
                while frame is not None and len(entries) < 16:
                    entries.append((Path(frame.f_code.co_filename).name,
                                    frame.f_code.co_name, frame.f_lineno))
                    frame = frame.f_back
                stacks[thread.name] = entries
        finally:
            frames.clear()
        self.failure_snapshot = {
            'failure_phase': phase,
            'records': tuple(self.records),
            'stacks': stacks,
            'worker_alive': worker.is_alive(),
            'worker_result_count': self.worker_result_count,
            'worker_error_types': tuple(self.worker_error_types),
            'watcher_alive': self.watcher.is_alive() if self.watcher is not None else None,
            'transport_unwound': self.transport_unwound,
            'http_client_closed': self.http_client.is_closed if self.http_client is not None else None,
            'connect_attempts': len(self.sockets),
            'open_connect_sockets': sum(sock.fileno() != -1 for sock in self.sockets),
        }
        return repr(self.failure_snapshot)


def _exercise_public_fixture_worker(trigger, tmp_path, monkeypatch, *, phases=None):
    """Public admission/direct worker/SQLite reopen; no RQ or external provider."""
    import os
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from apps.api.main import create_app
    from packages.persistence.store import Store
    from tests.integration.test_spine import PROFILE
    from workers import tasks

    phases = phases or _PublicWorkerPhases()
    worker_clock = _PendingConnectClock()
    store = Store(f'sqlite:///{(tmp_path / "fixture.db").as_posix()}')
    store.migrate()
    store.bind_workspace_mode('FIXTURE')
    store.save_profile(dict(PROFILE))
    worker = None
    observers = ExitStack()
    try:
        phases.observe(observers, store.url)
        # Replace this module's clock reference, never the shared time module.
        # Socket, database, admission and cleanup watchdogs retain real time.
        # The unchanged .8s/2s worker budget starts only after the fixture has
        # proved the pending-connect condition that this test is meant to cover.
        observers.enter_context(patch.object(tasks, 'time', worker_clock))
        with saturated_loopback_listener() as origin:
            connecting = threading.Event()
            sockets = []
            phases.sockets = sockets
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
            }), patch('workers.tasks.timeout', side_effect=lambda: phases.budget(2 if trigger == 'cancel' else .8)):
                admitted = api.post('/discovery', json={'profile_id': 'fixture', 'request_id': 'mock004-' + trigger})
                assert admitted.status_code == 202, admitted.text
                identifier = admitted.json()['operation_id']
                phases.operation_id = identifier
                results, errors = [], []

                def execute():
                    phases.record('worker_enter')
                    try:
                        results.append(tasks.execute_operation(identifier))
                        phases.worker_result_count = len(results)
                    except BaseException as error:
                        errors.append(error)
                        phases.worker_error_types.append(type(error).__name__)
                    finally:
                        phases.record('worker_exit')

                worker = threading.Thread(target=execute, name='mock004-public-worker')
                worker.start()
                assert connecting.wait(2), ('public worker did not attempt loopback connection; phases='
                                            + phases.describe_failure(worker, phase='connect_entry_failed'))
                time.sleep(.1)
                assert sockets and not select.select([], [sockets[-1]], [], 0)[1], 'TCP connect not pending'
                phases.record('pending_connect_confirmed')
                worker_clock.admit()
                phases.record('worker_deadline_admitted')
                if trigger == 'cancel':
                    assert api.post(f'/operations/{identifier}/cancel').status_code == 202
                phases.record('join_enter')
                worker.join(timeout=1.2)
                phases.record('join_exit')
                assert not worker.is_alive(), ('public fixture worker remained blocked in connect; phases='
                                               + phases.describe_failure(worker, phase='join_failed'))
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
        try:
            # A failed admission must still release the real worker to its
            # finite deadline before the existing cleanup watchdog waits.
            worker_clock.admit()
            phases.release_terminal.set()
            if worker is not None:
                worker.join(timeout=11)
                assert not worker.is_alive(), 'public worker survived finite cleanup watchdog'
            store.engine.dispose()
        finally:
            observers.close()
    assert not any(t.name == 'mjobs-fixture-discovery-cancellation' for t in threading.enumerate())


@pytest.mark.parametrize('trigger', ['cancel', 'deadline'])
def test_public_fixture_worker_pending_connect_persists_terminal_readback(trigger, tmp_path, monkeypatch):
    _exercise_public_fixture_worker(trigger, tmp_path, monkeypatch)


def test_public_worker_pending_connect_budget_excludes_slow_client_preparation(tmp_path, monkeypatch):
    """Constructor work longer than the budget cannot consume connect coverage."""
    original_init = MockClient.__init__
    preparation_seconds = []
    phases = _PublicWorkerPhases()

    def slow_init(client, *args, **kwargs):
        if threading.current_thread().name != 'mock004-public-worker':
            return original_init(client, *args, **kwargs)
        started = time.monotonic()
        time.sleep(.9)
        original_init(client, *args, **kwargs)
        preparation_seconds.append(time.monotonic() - started)

    with monkeypatch.context() as scoped:
        scoped.setattr(MockClient, '__init__', slow_init)
        _exercise_public_fixture_worker('deadline', tmp_path, scoped, phases=phases)
    assert len(preparation_seconds) == 1 and preparation_seconds[0] > .8
    labels = [record[0] for record in phases.records]
    assert labels.index('deadline_budget') < labels.index('mock_get_enter')
    assert labels.index('pending_connect_confirmed') < labels.index('worker_deadline_admitted')
    assert labels.count('backend_connect_OperationTimedOut') == 1
    assert phases.http_client.is_closed
    assert phases.worker_store.engine.pool.checkedout() == 0


def test_public_worker_entry_diagnostic_records_pre_connect_failure(tmp_path, monkeypatch):
    """A controlled resolver failure finishes without an observed OS connect."""
    from sqlalchemy import select
    from packages.persistence import models as m
    from packages.persistence.store import Store
    from services.discovery.sources import http_transport

    phases = _PublicWorkerPhases()
    private_message = 'controlled-private-diagnostic-value-must-not-appear'
    original_resolve = http_transport._resolve
    originals = (Store.__init__, http_transport._Backend.connect_tcp, MockClient.get,
                 MockClient.close, httpx.Client.close, socket.socket, original_resolve)

    def fail_before_connect(host, port, check, deadline):
        if threading.current_thread().name != 'mock004-public-worker':
            return original_resolve(host, port, check, deadline)
        phases.record('controlled_pre_connect_failure')
        raise RuntimeError(private_message)

    with monkeypatch.context() as scoped:
        scoped.setattr(http_transport, '_resolve', fail_before_connect)
        with pytest.raises(
            AssertionError, match='^public worker did not attempt loopback connection; phases='
        ) as failure:
            # Use the unchanged cancel case's existing budget. No cancellation
            # is issued: the entry predicate fails after the controlled error.
            _exercise_public_fixture_worker('cancel', tmp_path, scoped, phases=phases)

    snapshot = phases.failure_snapshot
    assert snapshot is not None
    assert snapshot['failure_phase'] == 'connect_entry_failed'
    assert not snapshot['worker_alive'] and snapshot['worker_result_count'] == 1
    assert not snapshot['worker_error_types']
    assert snapshot['watcher_alive'] in (None, False)
    assert snapshot['transport_unwound'] and snapshot['http_client_closed']
    assert snapshot['connect_attempts'] == snapshot['open_connect_sockets'] == 0
    assert snapshot['stacks']['mock004-public-worker'] == []
    labels = [record[0] for record in snapshot['records']]
    assert labels.count('controlled_pre_connect_failure') == 1, snapshot
    assert labels.count('backend_connect_RuntimeError') == 1, snapshot
    assert labels.index('backend_connect_exit') < labels.index('mock_get_exit')
    assert labels.index('mock_get_exit') < labels.index('store_dispose_exit')
    assert labels.index('store_dispose_exit') < labels.index('worker_exit')
    assert labels.index('worker_exit') < labels.index('connect_entry_failed')
    assert 'join_enter' not in labels
    assert len(snapshot['records']) <= 128
    assert set(snapshot['stacks']) <= {
        'mock004-public-worker', 'mjobs-fixture-discovery-cancellation',
    }
    assert all(len(frames) <= 16 for frames in snapshot['stacks'].values())
    assert private_message not in str(failure.value)
    assert private_message not in repr(snapshot)
    assert phases.worker_store.engine.pool.checkedout() == 0
    assert phases.http_client.is_closed
    assert phases.watcher is None or not phases.watcher.is_alive()
    assert not any(t.name == 'mjobs-fixture-discovery-cancellation' for t in threading.enumerate())
    assert originals == (Store.__init__, http_transport._Backend.connect_tcp, MockClient.get,
                         MockClient.close, httpx.Client.close, socket.socket, http_transport._resolve)

    reopened = Store(f'sqlite:///{(tmp_path / "fixture.db").as_posix()}')
    try:
        with reopened.engine.connect() as connection:
            row = connection.execute(select(
                m.operations.c.state, m.operations.c.error, m.operations.c.attempt_count,
            ).where(m.operations.c.id == phases.operation_id)).one()
        assert tuple(row) == ('DEAD_LETTERED', 'RuntimeError', 1)
    finally:
        reopened.engine.dispose()


def test_public_worker_phase_diagnostic_separates_terminal_commit_delay(tmp_path, monkeypatch):
    """A real pending connect stopped, but a held real terminal commit is late."""
    from sqlalchemy import select
    from packages.persistence import models as m
    from packages.persistence.store import Store

    phases = _PublicWorkerPhases(hold_terminal_commit=True)
    with pytest.raises(AssertionError, match='^public fixture worker remained blocked in connect; phases='):
        _exercise_public_fixture_worker('deadline', tmp_path, monkeypatch, phases=phases)

    snapshot = phases.failure_snapshot
    assert phases.terminal_held.is_set() and snapshot is not None
    labels = [record[0] for record in snapshot['records']]
    assert labels.count('backend_connect_OperationTimedOut') == 1, snapshot
    assert labels.index('backend_connect_exit') < labels.index('mock_get_exit')
    assert labels.index('mock_get_exit') < labels.index('terminal_commit_held') < labels.index('join_failed')
    assert snapshot['open_connect_sockets'] == 0, snapshot
    assert any(function == 'observed_commit'
               for _file, function, _line in snapshot['stacks']['mock004-public-worker']), snapshot
    completed = [record[0] for record in phases.records]
    assert completed.count('terminal_commit_returned') == 1, completed
    assert completed.index('terminal_commit_released') < completed.index('terminal_commit_returned'), completed
    assert completed.index('terminal_commit_returned') < completed.index('worker_exit'), completed
    assert not phases.worker_error_types and phases.worker_result_count == 1
    assert phases.watcher is None or not phases.watcher.is_alive()

    reopened = Store(f'sqlite:///{(tmp_path / "fixture.db").as_posix()}')
    try:
        with reopened.engine.connect() as connection:
            row = connection.execute(select(
                m.operations.c.state, m.operations.c.error, m.operations.c.attempt_count,
            ).where(m.operations.c.id == phases.operation_id)).one()
        assert tuple(row) == ('DEAD_LETTERED', 'OPERATION_TIMEOUT', 1)
    finally:
        reopened.engine.dispose()


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
