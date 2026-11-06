"""A stalled TLS handshake terminates durable advisory work without publication."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
import gc
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest import mock

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.ai.providers import AIProviderError
from services.ai.providers.transport import request_guard
from services.ai.tasks import AITaskRouter
from tests.integration import test_durable_ai_evaluation as evaluation_tests
from tests.unit.test_ai_tls_handshake import HTTPS_PROVIDERS, recorded_duplicates


class GCPhaseObserver:
    """Bounded, value-free observation; never change when collection happens."""

    def __init__(self, worker_thread):
        self.worker_thread = worker_thread
        # Keep older-generation evidence even if generation zero is frequent.
        self.events = {generation: deque(maxlen=4) for generation in (0, 1, 2)}
        self.started = {generation: 0 for generation in self.events}
        self.active = None

    def __enter__(self):
        gc.callbacks.append(self.observe)
        return self

    def __exit__(self, *_error):
        gc.callbacks.remove(self.observe)

    def observe(self, phase, info):
        # CPython serializes collections. Store only scalar timing/classification
        # data, never objects, locals, paths, or the callback's info dictionary.
        observed = time.monotonic()
        if phase == 'start':
            generation = info['generation']
            owned = bool(self.worker_thread and
                         threading.get_ident() == self.worker_thread[0])
            self.active = [observed, None, generation,
                           'owned_worker' if owned else 'other_thread']
            self.events[generation].append(self.active)
            self.started[generation] += 1
        elif phase == 'stop' and self.active is not None:
            self.active[1] = observed
            self.active = None

    def details(self, before):
        events = []
        snapshot = [event for bucket in self.events.values() for event in list(bucket)]
        for start, stop, generation, thread in sorted(snapshot, key=lambda event: event[0]):
            events.append({
                'generation': generation, 'thread': thread,
                'start_ms': round((start - before) * 1000, 3),
                'stop_ms': None if stop is None else round((stop - before) * 1000, 3),
                'duration_ms': None if stop is None else round((stop - start) * 1000, 3),
            })
        dropped = {generation: max(0, self.started[generation] - len(bucket))
                   for generation, bucket in self.events.items()}
        return f'gc_phases={events}; gc_dropped={dropped}'


class TimedCloseEvent(threading.Event):
    def __init__(self):
        super().__init__()
        self.closed_at = None
        self._timestamp_lock = threading.Lock()

    def set(self):
        with self._timestamp_lock:
            if self.closed_at is None:
                self.closed_at = time.monotonic()
            super().set()


@contextmanager
def stalled_tls_peer():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(3)
    started, finish = (threading.Event() for _ in range(2))
    closed = TimedCloseEvent()
    errors = []
    def peer():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(.05)
                while not finish.is_set():
                    try:
                        data = connection.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        closed.set()
                        return
                    started.set()  # ClientHello reached the real TLS boundary.
        except (ConnectionResetError, ConnectionAbortedError):
            closed.set()
        except Exception as error:
            errors.append(type(error).__name__)
    thread = threading.Thread(target=peer)
    thread.start()
    try:
        yield f'https://127.0.0.1:{listener.getsockname()[1]}', started, closed
    finally:
        finish.set()
        thread.join(3)
        listener.close()
        if thread.is_alive() or errors:
            raise AssertionError(f'TLS fixture cleanup failed: {errors}')


class DurableTLSRecoveryTests(unittest.TestCase):
    # Reuse the real API/database fixture, without inheriting unrelated tests.
    setUp = evaluation_tests.DurableAIEvaluationTests.setUp
    new_client = evaluation_tests.DurableAIEvaluationTests.new_client
    run_worker = evaluation_tests.DurableAIEvaluationTests.run_worker

    def test_rolled_back_cancellation_does_not_interrupt_inflight_tls(self):
        provider_type = next(provider for provider in HTTPS_PROVIDERS
                             if provider.name == 'openai')
        with stalled_tls_peer() as (origin, started, closed), recorded_duplicates() as duplicates:
            client = httpx.Client(trust_env=False, timeout=1.5)
            self.addCleanup(client.close)
            provider = provider_type(base_url=origin, client_factory=lambda **_kwargs: client)

            class BoundedProvider:
                capabilities = provider.capabilities

                def generate(self, **kwargs):
                    kwargs['timeout_seconds'] = 1
                    return provider.generate(**kwargs)

            self.router = lambda configuration: AITaskRouter(
                configuration, provider_factory=lambda _name: BoundedProvider(),
            )
            admitted = self.client.post(
                '/profiles/fixture/ai-cost-plan/evaluate/queued',
                json={'request_id': 'rolled-back-cancellation'},
            )
            self.assertEqual(admitted.status_code, 202, admitted.text)
            operation_id = admitted.json()['operation_id']
            path = '/profiles/fixture/ai-cost-plan/operations/' + operation_id
            original_audit = self.store._audit

            def reject_cancellation_audit(connection, entity_id, action, payload):
                if action == 'CANCELLATION_REQUESTED':
                    raise RuntimeError('synthetic cancellation rollback')
                return original_audit(connection, entity_id, action, payload)

            with ThreadPoolExecutor(max_workers=2) as pool:
                work = pool.submit(self.run_worker, operation_id)
                self.assertTrue(started.wait(3), 'TLS ClientHello never reached the peer')
                with mock.patch.object(
                        self.store, '_audit', side_effect=reject_cancellation_audit):
                    cancelling = pool.submit(self.client.post, path + '/cancel')
                    with self.assertRaisesRegex(RuntimeError, 'synthetic cancellation rollback'):
                        cancelling.result(timeout=1)
                self.assertFalse(closed.wait(.2),
                                 'rolled-back cancellation interrupted the TLS call')
                self.assertFalse(work.done(),
                                 'rolled-back cancellation terminally failed the worker')
                result = work.result(timeout=2)

            self.assertEqual(result['status'], 'BLOCKED', result)
            self.assertEqual(result['error_category'], 'AI_ERROR')
            self.assertTrue(closed.wait(.4), 'provider timeout did not close the TLS peer')
            self.assertEqual(len(duplicates), 1)
            self.assertEqual(duplicates[0].fileno(), -1)
            self.assertEqual(self.store.ai_evaluation_history('fixture'), [])
            with self.store.engine.connect() as connection:
                row = connection.execute(select(
                    m.operations.c.state, m.operations.c.cancel_requested,
                ).where(m.operations.c.id == operation_id)).one()
            self.assertEqual((row.state, row.cancel_requested), ('BLOCKED', False))

    def test_request_owner_finishes_cleanup_before_guard_returns(self):
        watcher_close_started = threading.Event()
        release_watcher_close = threading.Event()
        cancellation = threading.Event()
        request_started = threading.Event()

        class TrackedResource:
            def __init__(self):
                self.closed = threading.Event()
                self.close_threads = []

            def get_extra_info(self, _name):
                return None

            def close(self):
                self.close_threads.append(threading.current_thread().name)
                if threading.current_thread().name == 'mjobs-request-deadline':
                    watcher_close_started.set()
                    release_watcher_close.wait(2)
                self.closed.set()

        class Client(TrackedResource):
            _transport = None
            _mounts = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        client = Client()
        response = TrackedResource()
        stream = TrackedResource()

        def run_request():
            try:
                with client, request_guard(client, deadline=time.monotonic() + 2,
                                           cancellation_event=cancellation) as guard:
                    guard.response = response
                    guard.stream = stream
                    request_started.set()
                    while not guard.interrupted:
                        time.sleep(.001)
                    guard.check()
            except AIProviderError as error:
                return error.code

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                work = pool.submit(run_request)
                self.assertTrue(request_started.wait(.2))
                cancellation.set()
                self.assertEqual(work.result(timeout=.5), 'AI_PROVIDER_CANCELLED')
            self.assertTrue(response.closed.is_set())
            self.assertTrue(stream.closed.is_set())
            self.assertTrue(client.closed.is_set())
            self.assertFalse(any(t.name == 'mjobs-request-deadline'
                                 for t in threading.enumerate()),
                             'request returned with watcher-owned cleanup still live')
            self.assertFalse(any('mjobs-request-deadline' in resource.close_threads
                                 for resource in (response, stream, client)),
                             'watcher performed a potentially blocking high-level close')
        finally:
            release_watcher_close.set()
            deadline = time.monotonic() + .3
            while (any(t.name == 'mjobs-request-deadline' for t in threading.enumerate())
                   and time.monotonic() < deadline):
                time.sleep(.005)

    def test_tls_cancellation_and_deadline_persist_without_uncertain_replay(self):
        for provider_type in HTTPS_PROVIDERS:
            for cancel in (False, True):
                # Isolate prior fixture cycles before creating any operation or
                # deadline. Automatic GC stays enabled throughout the operation.
                gc_setup_started = time.monotonic()
                gc_setup_collected = gc.collect()
                gc_setup_ms = (time.monotonic() - gc_setup_started) * 1000
                with self.subTest(provider=provider_type.name, cancel=cancel), stalled_tls_peer() as (
                        origin, started, closed), recorded_duplicates() as duplicates, ExitStack() as clients:
                    client = httpx.Client(trust_env=False, timeout=1.5)
                    self.addCleanup(client.close)
                    provider = provider_type(base_url=origin, client_factory=lambda **_kwargs: client)
                    budget = 4 if cancel else .3
                    class BoundedProvider:
                        capabilities = provider.capabilities
                        def generate(self, **kwargs):
                            kwargs['timeout_seconds'] = budget
                            return provider.generate(**kwargs)
                    self.router = lambda configuration: AITaskRouter(configuration,
                        provider_factory=lambda _name: BoundedProvider())
                    admitted = self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued',
                        json={'request_id': f'tls-handshake-{provider_type.name}-{cancel}'})
                    self.assertEqual(admitted.status_code, 202, admitted.text)
                    operation_id = admitted.json()['operation_id']
                    path = '/profiles/fixture/ai-cost-plan/operations/' + operation_id
                    cancel_client = None
                    cancel_client_setup_ms = None
                    if cancel:
                        setup_started = time.monotonic()
                        # Start the ASGI portal before the timed TLS phase;
                        # constructing TestClient alone leaves it lazy.
                        cancel_client = clients.enter_context(self.new_client())
                        ready = cancel_client.get(path)
                        self.assertEqual(ready.status_code, 200, ready.text)
                        self.assertEqual(ready.json()['state'], 'QUEUED')
                        cancel_client_setup_ms = (time.monotonic() - setup_started) * 1000
                    worker_thread, worker_finished = [], []

                    def run_recorded_worker(operation_id, worker_thread, worker_finished):
                        worker_thread.append(threading.get_ident())
                        try:
                            return self.run_worker(operation_id)
                        finally:
                            worker_finished.append(time.monotonic())

                    try:
                        with GCPhaseObserver(worker_thread) as gc_phases, \
                                ThreadPoolExecutor(max_workers=2) as pool:
                            work = pool.submit(
                                run_recorded_worker, operation_id, worker_thread, worker_finished)
                            self.assertTrue(started.wait(3), 'TLS ClientHello never reached the peer')
                            before = time.monotonic()
                            acknowledged_at = None
                            if cancel:
                                cancellation_staged = threading.Event()
                                release_cancellation = threading.Event()
                                original_audit = self.store._audit

                                def pause_cancellation_audit(connection, entity_id, action, payload):
                                    if action == 'CANCELLATION_REQUESTED':
                                        cancellation_staged.set()
                                        release_cancellation.wait(1)
                                    return original_audit(connection, entity_id, action, payload)

                                with mock.patch.object(
                                        self.store, '_audit', side_effect=pause_cancellation_audit):
                                    cancelling = pool.submit(cancel_client.post, path + '/cancel')
                                    try:
                                        self.assertTrue(cancellation_staged.wait(.5),
                                                        'cancel transaction was not staged')
                                        closed_before_commit = closed.wait(.2)
                                    finally:
                                        release_cancellation.set()
                                    response = cancelling.result(timeout=1)
                                acknowledged_at = time.monotonic()
                                self.assertEqual(response.status_code, 200, response.text)
                                self.assertFalse(
                                    closed_before_commit,
                                    'uncommitted cancellation interrupted TLS before commit',
                                )
                            # Sample only a late test-owned worker; preserve the
                            # original result-wait budget and terminal assertion.
                            result_deadline = time.monotonic() + 3
                            done, _ = wait([work], timeout=max(
                                0, before + .75 - time.monotonic()))
                            worker_stack, probe_ms = [], None
                            if not done:
                                probe_ms = round((time.monotonic() - before) * 1000, 3)
                                frame = sys._current_frames().get(worker_thread[0])
                                try:
                                    while frame is not None and len(worker_stack) < 12:
                                        worker_stack.append(
                                            f'{Path(frame.f_code.co_filename).name}:'
                                            f'{frame.f_lineno}:{frame.f_code.co_name}')
                                        frame = frame.f_back
                                finally:
                                    del frame
                                print(f'durable-ai-tls-pending-worker provider={provider_type.name} '
                                      f'cancel={cancel}; probe_ms={probe_ms}; '
                                      f'pending_worker_stack={worker_stack}; '
                                      f'{gc_phases.details(before)}')
                            result = work.result(timeout=max(
                                0, result_deadline - time.monotonic()))
                            terminal_at = time.monotonic()
                            self.assertFalse(any(t.name == 'mjobs-request-deadline'
                                                 for t in threading.enumerate()),
                                             'durable terminal state preceded watcher cleanup')
                            self.assertTrue(client.is_closed,
                                            'durable terminal state preceded client cleanup')
                            self.assertEqual(len(duplicates), 1)
                            self.assertEqual(duplicates[0].fileno(), -1,
                                             'durable terminal state retained interruption socket')
                            response_closed = closed.wait(.4)
                            response_closed_at = closed.closed_at
                            elapsed = terminal_at - before
                            setup_phase = ('n/a' if cancel_client_setup_ms is None else
                                           f'{cancel_client_setup_ms:.1f}ms (excluded)')
                            ack_phase = ('n/a' if acknowledged_at is None else
                                         f'{(acknowledged_at - before) * 1000:.1f}ms')
                            close_phase = ('not observed' if response_closed_at is None else
                                           f'{(response_closed_at - before) * 1000:.1f}ms')
                            phase_details = (f'provider={provider_type.name} cancel={cancel}; '
                                             f'client_setup={setup_phase}; ack={ack_phase}; '
                                             f'gc_setup={gc_setup_ms:.3f}ms (before operation); '
                                             f'gc_setup_collected={gc_setup_collected}; '
                                             f'socket_close={close_phase}; '
                                             f'worker_terminal={elapsed * 1000:.1f}ms; '
                                             f'worker_exit={(worker_finished[0] - before) * 1000:.1f}ms; '
                                             f'probe_ms={probe_ms}; pending_worker_stack={worker_stack}; '
                                             f'{gc_phases.details(before)}')
                            print('durable-ai-tls-timing ' + phase_details)
                            self.assertLess(elapsed, .9, phase_details)
                    finally:
                        if cancel_client is not None:
                            clients.close()
                            cancel_client.close()
                    expected = 'CANCELLED' if cancel else 'BLOCKED'
                    self.assertEqual(result['status'], expected, result)
                    if not cancel:
                        self.assertEqual(result['error_category'], 'AI_ERROR')
                    self.assertTrue(response_closed, 'TLS peer remained connected')
                    self.assertEqual(self.store.ai_evaluation_history('fixture'), [])
                    reopened = Store(self.url)
                    try:
                        with reopened.engine.connect() as connection:
                            state = connection.execute(select(m.operations.c.state).where(
                                m.operations.c.id == operation_id)).scalar_one()
                            self.assertEqual(state, expected)
                        self.assertEqual(reopened.ai_evaluation_history('fixture'), [])
                    finally:
                        reopened.engine.dispose()
                    readback = self.new_client().get(path)
                    self.assertEqual(readback.status_code, 200, readback.text)
                    self.assertEqual(readback.json()['state'], expected)
                    if not cancel:
                        self.assertEqual(readback.json()['error'], 'AI_ERROR')
                    # Redelivering a terminal operation cannot reopen its uncertain call.
                    self.assertEqual(self.run_worker(operation_id)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
                    self.assertEqual(len(duplicates), 1)
                    self.assertFalse(any(t.name == 'mjobs-request-deadline' for t in threading.enumerate()))
