"""A stalled TLS handshake terminates durable advisory work without publication."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import socket
import threading
import time
import unittest

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.ai.providers.transport import request_guard
from services.ai.tasks import AITaskRouter
from tests.integration import test_durable_ai_evaluation as evaluation_tests
from tests.unit.test_ai_tls_handshake import HTTPS_PROVIDERS, recorded_duplicates


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

    def test_guard_completion_does_not_wait_for_blocked_watcher_cleanup(self):
        watcher_close_started = threading.Event()
        watcher_close_finished = threading.Event()
        release_watcher_close = threading.Event()
        cancellation = threading.Event()

        class BlockingStream:
            def get_extra_info(self, _name):
                return None

            def close(self):
                if threading.current_thread().name == 'mjobs-request-deadline':
                    watcher_close_started.set()
                    release_watcher_close.wait(2)
                    watcher_close_finished.set()

        class Client:
            _transport = None
            _mounts = {}

            def close(self):
                pass

        try:
            with request_guard(Client(), deadline=time.monotonic() + 2,
                               cancellation_event=cancellation) as guard:
                guard.stream = BlockingStream()
                cancellation.set()
                self.assertTrue(watcher_close_started.wait(.2))
                # The watcher has already recorded the interruption. Clearing
                # the synthetic signal lets the owner exercise only finalization.
                cancellation.clear()
                before = time.monotonic()
            self.assertLess(time.monotonic() - before, .2,
                            'request completion waited for watcher-owned cleanup')
        finally:
            release_watcher_close.set()
            self.assertTrue(watcher_close_finished.wait(.2))

    def test_tls_cancellation_and_deadline_persist_without_uncertain_replay(self):
        for provider_type in HTTPS_PROVIDERS:
            for cancel in (False, True):
                with self.subTest(provider=provider_type.name, cancel=cancel), stalled_tls_peer() as (
                        origin, started, closed), recorded_duplicates() as duplicates:
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
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        work = pool.submit(self.run_worker, operation_id)
                        self.assertTrue(started.wait(3), 'TLS ClientHello never reached the peer')
                        before = time.monotonic()
                        acknowledged_at = None
                        if cancel:
                            response = self.new_client().post(path + '/cancel')
                            acknowledged_at = time.monotonic()
                            self.assertEqual(response.status_code, 200, response.text)
                        result = work.result(timeout=3)
                        terminal_at = time.monotonic()
                        response_closed = closed.wait(.4)
                        response_closed_at = closed.closed_at
                        elapsed = terminal_at - before
                        ack_phase = ('n/a' if acknowledged_at is None else
                                     f'{(acknowledged_at - before) * 1000:.1f}ms')
                        close_phase = ('not observed' if response_closed_at is None else
                                       f'{(response_closed_at - before) * 1000:.1f}ms')
                        phase_details = (f'provider={provider_type.name} cancel={cancel}; '
                                         f'ack={ack_phase}; socket_close={close_phase}; '
                                         f'worker_terminal={elapsed * 1000:.1f}ms')
                        print('durable-ai-tls-timing ' + phase_details)
                        self.assertLess(elapsed, .9, phase_details)
                    expected = 'CANCELLED' if cancel else 'BLOCKED'
                    self.assertEqual(result['status'], expected, result)
                    if not cancel:
                        self.assertEqual(result['error_category'], 'AI_PROVIDER_TIMEOUT')
                    self.assertTrue(response_closed, 'TLS peer remained connected')
                    self.assertEqual(len(duplicates), 1)
                    self.assertEqual(duplicates[0].fileno(), -1)
                    self.assertTrue(client.is_closed)
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
                        self.assertEqual(readback.json()['error'], 'AI_PROVIDER_TIMEOUT')
                    # Redelivering a terminal operation cannot reopen its uncertain call.
                    self.assertEqual(self.run_worker(operation_id)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
                    self.assertEqual(len(duplicates), 1)
                    self.assertFalse(any(t.name == 'mjobs-request-deadline' for t in threading.enumerate()))
