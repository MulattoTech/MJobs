"""Shared-route ownership across independent applications and worker stores."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications import attempt_history as history
from services.applications.run_requests import queue_mock_run
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation
from workers.operations import recover_expired


class RouteAttemptConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mjobs-route-owner-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.url = f'sqlite:///{(self.root / "fixture.db").as_posix()}'
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.receipts = self.root / 'receipts.json'
        server = fixture_server(self.receipts)
        self.origin = server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        self.app_ids = []
        for profile_id in ('fixture', 'fixture-other'):
            profile = deepcopy(PROFILE)
            profile['id'] = profile_id
            self.store.save_profile(profile)
            discover(self.store, self.origin, profile_id)
            self.app_ids.append(prepare(self.store, self.store.jobs(profile_id)[0]['id'],
                                        profile_id, self.root))
        self.assertEqual(*(self.store.application(i)['route_id'] for i in self.app_ids))

    def reserve(self, index, request, store=None):
        store = store or self.store
        app = store.application(self.app_ids[index])
        with store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == app['route_id'])).mappings().one())
            docs = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == app['id'],
                m.documents.c.version == app['document_version'])).mappings())
        return history.start_attempt(store, app, route, docs, request_id=request)

    def test_independent_connections_race_and_restart_cannot_steal_route(self):
        barrier = threading.Barrier(2)
        def compete(index):
            store = Store(self.url)
            try:
                barrier.wait(timeout=10)
                try:
                    return index, self.reserve(index, 'parallel', store)['id']
                except history.AttemptConflict as error:
                    return index, error.code
            finally:
                store.engine.dispose()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, (0, 1)))
        self.assertEqual(sum(value == 'APPLICATION_ROUTE_IN_PROGRESS' for _, value in results), 1)
        winner, attempt = next((i, value) for i, value in results if len(value) == 32)
        loser = 1 - winner
        self.assertEqual(self.store.count('application_attempts'), 1)
        restarted = Store(self.url)
        try:
            with self.assertRaisesRegex(history.AttemptConflict, 'APPLICATION_ROUTE_IN_PROGRESS'):
                self.reserve(loser, 'after-restart', restarted)
            # Mutable application metadata cannot release the original route.
            with self.store.engine.begin() as connection:
                other_route = connection.execute(select(m.routes.c.id).where(
                    m.routes.c.id != self.store.application(self.app_ids[winner])['route_id'])).scalar()
                self.assertIsNotNone(other_route)
                connection.execute(update(m.applications).where(
                    m.applications.c.id == self.app_ids[winner]).values(route_id=other_route))
            with self.assertRaisesRegex(history.AttemptConflict, 'APPLICATION_ROUTE_IN_PROGRESS'):
                self.reserve(loser, 'after-route-edit', restarted)
            history.finish_attempt(self.store, attempt, self.store.application(self.app_ids[winner]),
                                   error=RuntimeError('Synthetic stopped browser'))
            self.assertFalse(self.reserve(loser, 'new-reviewed-intent', restarted)['replayed'])
        finally:
            restarted.engine.dispose()

    def test_two_real_workers_only_one_submits_then_explicit_retry_is_safe(self):
        operations = [queue_mock_run(self.store, self.store.application(i)['profile_id'], i,
                      submit=True, request_id='explicit-submit')['operation_id'] for i in self.app_ids]
        env = {'MJOBS_DATABASE_URL': self.url, 'MJOBS_OUTPUT_DIR': str(self.root),
               'MJOBS_MOCK_ORIGIN': self.origin, 'MJOBS_OPERATION_MODE': 'FIXTURE',
               'MJOBS_APPLICATION_AUTOMATION_CONCURRENCY': '2'}
        gate = self.root / 'worker-race'
        gate.mkdir()
        code = '''import json,sys,time
from pathlib import Path
from services.applications import attempt_history as history
from workers.tasks import execute_operation
gate=Path(sys.argv[2]); original=history.start_attempt
def wait_for(predicate):
    deadline=time.monotonic()+30
    while not predicate():
        if time.monotonic()>deadline: raise AssertionError('Route race synchronization failed')
        time.sleep(0.02)
def race(*args, **kwargs):
    (gate/(sys.argv[1]+'.arrived')).write_text('ready')
    wait_for(lambda:len(list(gate.glob('*.arrived')))==2)
    try: result=original(*args, **kwargs)
    except history.AttemptConflict:
        (gate/'blocked').write_text('blocked')
        raise
    wait_for(lambda:(gate/'blocked').exists())
    return result
history.start_attempt=race
print(json.dumps(execute_operation(sys.argv[1])))
'''
        def run_child(operation):
            result = subprocess.run([sys.executable, '-c', code, operation, str(gate)],
                cwd=Path(__file__).resolve().parents[2], env={**os.environ, **env},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            return json.loads(result.stdout.strip().splitlines()[-1])
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run_child, operations))
        self.assertEqual(sorted(r['status'] for r in outcomes), ['BLOCKED', 'COMPLETED'])
        with self.store.engine.connect() as connection:
            rows = list(connection.execute(select(m.operations).where(
                m.operations.c.id.in_(operations))).mappings())
        self.assertEqual(sorted(r['state'] for r in rows), ['BLOCKED', 'COMPLETED'])
        blocked = next(r for r in rows if r['state'] == 'BLOCKED')
        self.assertEqual(blocked['error'], 'APPLICATION_ROUTE_IN_PROGRESS')
        receipts = json.loads(self.receipts.read_text())
        self.assertEqual(len(receipts), 1)
        self.assertEqual(self.store.count('application_attempts'), 1)
        self.assertNotIn(blocked['entity_id'], receipts)
        self.assertEqual(httpx.get(self.origin + '/_test/request-counts', trust_env=False).json()['submit'], 1)
        with patch.dict(os.environ, env):
            self.assertEqual(execute_operation(blocked['id'])['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
            app = self.store.application(blocked['entity_id'])
            retry = queue_mock_run(self.store, app['profile_id'], app['id'], submit=True,
                                   request_id='reviewed-after-other-worker')['operation_id']
            execute_operation(retry)
        self.assertEqual(self.store.application(app['id'])['state'], 'CONFIRMED')
        self.assertEqual(len(json.loads(self.receipts.read_text())), 2)
        self.assertEqual(httpx.get(self.origin + '/_test/request-counts', trust_env=False).json()['submit'], 2)

    def test_other_routes_remain_available_and_unknown_legacy_owner_fails_closed(self):
        first = self.reserve(0, 'first-route')
        other_job = next(job for job in self.store.jobs('fixture-other')
                         if job['id'] != self.store.application(self.app_ids[1])['job_id'])
        other_app = prepare(self.store, other_job['id'], 'fixture-other', self.root)
        self.app_ids[1] = other_app
        self.assertNotEqual(self.store.application(other_app)['route_id'],
                            self.store.application(self.app_ids[0])['route_id'])
        second = self.reserve(1, 'independent-route')
        history.finish_attempt(self.store, second['id'], self.store.application(other_app),
                               error=RuntimeError('Synthetic browser closed'))
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.delete().where(
                m.audit.c.id == history._event_id(history.STARTED, first['id'])))
        with self.assertRaisesRegex(history.AttemptConflict, 'APPLICATION_ROUTE_REVIEW_REQUIRED'):
            self.reserve(1, 'cannot-infer-legacy-route')

    def test_completed_replay_does_not_reacquire_route_held_by_another_application(self):
        first = self.reserve(0, 'original')
        self.store.transition(self.app_ids[0], ['PREPARED'], 'DRY_RUN_COMPLETE')
        history.finish_attempt(self.store, first['id'], self.store.application(self.app_ids[0]))
        self.reserve(1, 'other-browser')
        self.assertTrue(self.reserve(0, 'original')['replayed'])
        self.assertEqual(self.store.count('application_attempts'), 2)

    def test_receipt_recovery_releases_only_proven_fenced_confirmed_owner(self):
        app = self.store.application(self.app_ids[0])
        operation = queue_mock_run(self.store, app['profile_id'], app['id'], submit=True,
                                   request_id='crash-after-submit')['operation_id']
        env = {'MJOBS_DATABASE_URL': self.url, 'MJOBS_OUTPUT_DIR': str(self.root),
               'MJOBS_MOCK_ORIGIN': self.origin, 'MJOBS_OPERATION_MODE': 'FIXTURE'}
        # Exit the test-owned worker after provider side effect. SystemExit still
        # runs browser/client cleanup, but suppress the immutable finish receipt to
        # reproduce the durable crash state safely without leaving orphan browsers.
        code = '''import sys
from services.applications import attempt_history, mock_adapter
from workers.tasks import execute_operation
attempt_history.finish_attempt=lambda *args, **kwargs: None
def stopped(*args, **kwargs): raise SystemExit(73)
mock_adapter.record_mock_application_submitted=stopped
execute_operation(sys.argv[1])
'''
        child = subprocess.run([sys.executable, '-c', code, operation],
            cwd=Path(__file__).resolve().parents[2], env={**os.environ, **env},
            capture_output=True, text=True, timeout=90,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(child.returncode, 73, child.stderr[-2000:])
        before = history.read_attempt_history(self.store, app['profile_id'], app['id'])
        self.assertEqual(before['attempts'][0]['state'], 'RUNNING')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation).values(
                lease_expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()))
        self.assertEqual(recover_expired(self.store), 1)
        with patch.dict(os.environ, env), patch('services.applications.mock_adapter._receipt', return_value=None):
            self.assertEqual(execute_operation(operation)['status'], 'BLOCKED')
        self.assertEqual(self.store.application(app['id'])['state'], 'SUBMISSION_UNCONFIRMED')
        with self.assertRaisesRegex(history.AttemptConflict, 'APPLICATION_ROUTE_IN_PROGRESS'):
            self.reserve(1, 'ambiguous-owner')
        from services.applications.confirmation_recovery import reconcile_confirmation
        result = reconcile_confirmation(self.store, app['profile_id'], app['id'], self.origin,
                                        request_id='explicit-receipt-check', operation_mode='FIXTURE')
        self.assertTrue(result['provider_receipt_verified'])
        # A bare confirmed state is insufficient if the worker fence disappears.
        with self.store.engine.begin() as connection:
            event = dict(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == operation, m.audit.c.action == 'LEASE_RECOVERY')).mappings().one())
            connection.execute(m.audit.delete().where(m.audit.c.id == event['id']))
        with self.assertRaisesRegex(history.AttemptConflict, 'APPLICATION_ROUTE_IN_PROGRESS'):
            self.reserve(1, 'missing-recovery-proof')
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(**event))
        self.assertFalse(self.reserve(1, 'after-verified-recovery')['replayed'])
        self.assertEqual(history.read_attempt_history(self.store, app['profile_id'], app['id']), before)
        self.assertEqual(httpx.get(self.origin+'/_test/request-counts', trust_env=False).json()['submit'], 1)
