"""Real worker claims and commit/ack recovery for direct fixture monitoring.

SQLite and owned loopback HTTP exercise the production worker path; this is
not a PostgreSQL/RQ/container acceptance claim.
"""
from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx

from sqlalchemy import select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery import fixture_monitoring as monitor
from services.company_discovery.monitoring import source_version
from services.discovery.pipeline import discover, MockClient
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE
from workers.operations import MAX_ATTEMPTS, recover_expired, request_cancel
from workers.tasks import execute_operation


class FixtureMonitoringWorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.directory.name) / 'worker.db').as_posix()
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(PROFILE)
        save_search_profile(self.store, dict(id='all', profile_id='fixture', name='All', enabled=True, source_ids=[]))
        self.server = fixture_server()
        self.origin = self.server.__enter__()
        discover(self.store, self.origin, 'fixture')
        with self.store.engine.connect() as connection:
            self.source = dict(connection.execute(select(m.company_sources)).mappings().one())
        configured = monitor.configure_fixture_monitoring(self.store, self.origin, 'fixture', self.source['id'],
            source_version(self.source), 0, 300)
        self.operation_id = monitor.schedule_fixture_sources(self.store, self.origin,
            at=datetime.fromisoformat(configured['next_due']))['scheduled'][0]['operation_id']
        self.env = dict(MJOBS_OPERATION_MODE='FIXTURE', MJOBS_DATABASE_URL=self.url,
            MJOBS_MOCK_ORIGIN=self.origin, MJOBS_OUTPUT_DIR=self.directory.name)

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.store.engine.dispose()
        self.directory.cleanup()

    def operation(self):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(m.operations.c.id == self.operation_id)).mappings().one())

    def run_worker(self):
        with patch.dict(os.environ, self.env):
            return execute_operation(self.operation_id)

    def receipt_count(self):
        with self.store.engine.connect() as connection:
            return len(connection.execute(select(m.audit).where(m.audit.c.action == monitor.RECEIPT)).all())

    def expire_and_reopen(self, **values):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == self.operation_id).values(
                lease_expires_at='2000-01-01T00:00:00+00:00', **values))
        reopened = Store(self.url)
        try:
            recover_expired(reopened)
        finally:
            reopened.engine.dispose()

    def test_real_worker_dispatches_exact_source_and_duplicate_delivery_is_inert(self):
        paths = []
        original = MockClient.get
        def get(client, url):
            paths.append(url)
            return original(client, url)
        with patch.object(MockClient, 'get', get), patch('workers.tasks.discover_company_source') as generic:
            result = self.run_worker()
            duplicate = self.run_worker()
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(duplicate['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(paths, [self.source['endpoint']])
        generic.assert_not_called()
        self.assertEqual(self.operation()['attempt_count'], 1)
        self.assertEqual(self.receipt_count(), 1)

    def test_committed_receipt_overrides_finalization_exception_and_late_cancel_flag(self):
        original = monitor.refresh_fixture_source
        def committed_then_error(*args, **kwargs):
            result = original(*args, **kwargs)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == self.operation_id).values(cancel_requested=True))
            raise RuntimeError('synthetic postcommit finalization failure')
        with patch.object(monitor, 'refresh_fixture_source', committed_then_error):
            result = self.run_worker()
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertFalse(self.operation()['cancel_requested'])
        self.assertEqual(self.receipt_count(), 1)

    def test_process_exit_after_commit_rejects_cancel_and_recovers_exhausted_lease_without_replay(self):
        original = monitor.refresh_fixture_source
        def committed_then_exit(*args, **kwargs):
            original(*args, **kwargs)
            raise SystemExit('synthetic process exit after durable commit')
        with patch.object(monitor, 'refresh_fixture_source', committed_then_exit):
            with self.assertRaises(SystemExit):
                self.run_worker()
        self.assertEqual(self.operation()['state'], 'RUNNING')
        self.assertEqual(request_cancel(self.store, self.operation_id)['reason'], 'REFRESH_ALREADY_SAVED')
        self.expire_and_reopen(attempt_count=MAX_ATTEMPTS)
        self.assertEqual(self.operation()['state'], 'COMPLETED')
        self.assertFalse(self.operation()['cancel_requested'])
        with patch.object(MockClient, 'get', side_effect=AssertionError('unexpected replay')):
            self.assertEqual(self.run_worker()['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(self.receipt_count(), 1)

    def test_process_exit_before_import_does_not_fabricate_receipt_and_retries_real_worker(self):
        with patch.object(monitor, 'refresh_fixture_source', side_effect=SystemExit('before import')):
            with self.assertRaises(SystemExit):
                self.run_worker()
        self.assertEqual(self.receipt_count(), 0)
        self.expire_and_reopen()
        self.assertEqual(self.operation()['state'], 'QUEUED')
        self.assertEqual(self.run_worker()['status'], 'COMPLETED')
        self.assertEqual(self.operation()['attempt_count'], 2)
        self.assertEqual(self.receipt_count(), 1)

    def test_receipt_with_changed_parameters_cannot_certify_expired_operation(self):
        original = monitor.refresh_fixture_source
        def committed_then_exit(*args, **kwargs):
            original(*args, **kwargs)
            raise SystemExit('after commit')
        with patch.object(monitor, 'refresh_fixture_source', committed_then_exit):
            with self.assertRaises(SystemExit):
                self.run_worker()
        params = {**self.operation()['params'], 'profile_id': 'different-profile'}
        self.expire_and_reopen(params=params, attempt_count=MAX_ATTEMPTS)
        self.assertEqual(self.operation()['state'], 'DEAD_LETTERED')
        self.assertEqual(self.receipt_count(), 1)

    def test_malformed_marker_never_falls_back_to_generic_discovery(self):
        params = {**self.operation()['params'], 'fixture_monitoring': None,
            'candidate': {'company_name': 'Example Systems Inc.', 'company_website': self.origin+'/company'}}
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == self.operation_id).values(params=params))
        with patch('workers.tasks.discover_company_source') as generic, patch.object(MockClient, 'get') as network:
            result = self.run_worker()
        self.assertNotEqual(result['status'], 'COMPLETED')
        generic.assert_not_called()
        network.assert_not_called()
        self.assertEqual(self.receipt_count(), 0)

    def test_personal_mode_cannot_dispatch_fixture_marker_even_with_public_fetch_label(self):
        params = {**self.operation()['params'], 'fetch_mode': 'PUBLIC_HTTPS',
            'candidate': {'company_name': 'Example Systems Inc.', 'company_website': 'https://example.invalid'}}
        with self.store.engine.begin() as connection:
            # Negative control: model an invalid operation crossing workspace boundaries.
            connection.execute(update(m.workspace_identity).values(operation_mode='PERSONAL'))
            connection.execute(update(m.operations).where(m.operations.c.id == self.operation_id).values(params=params))
        self.env['MJOBS_OPERATION_MODE'] = 'PERSONAL'
        with patch('workers.tasks.PublicCompanyClient') as public, patch('workers.tasks.discover_company_source') as generic:
            result = self.run_worker()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'FIXTURE_OPERATION_DISABLED')
        public.assert_not_called()
        generic.assert_not_called()

    def test_cancel_during_direct_fetch_rolls_back_import(self):
        before = self.store.jobs('fixture')
        original = MockClient.get
        def get(client, url):
            response = original(client, url)
            request_cancel(self.store, self.operation_id)
            return response
        with patch.object(MockClient, 'get', get):
            result = self.run_worker()
        self.assertEqual(result['status'], 'CANCELLED', result)
        self.assertEqual(self.store.jobs('fixture'), before)
        self.assertEqual(self.receipt_count(), 0)

    def test_transient_failure_retries_same_operation_without_early_redelivery(self):
        with patch.object(MockClient, 'get', side_effect=httpx.ConnectError('synthetic outage')):
            result = self.run_worker()
        self.assertEqual(result['status'], 'QUEUED', result)
        self.assertEqual(self.operation()['retry_count'], 1)
        self.assertIsNotNone(self.operation()['next_attempt_at'])
        self.assertEqual(self.receipt_count(), 0)
        with patch.object(MockClient, 'get', side_effect=AssertionError('premature retry')):
            self.assertEqual(self.run_worker()['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        with self.store.engine.begin() as connection:
            # Unit-test clock control only; acceptance must wait the real deadline.
            connection.execute(update(m.operations).where(m.operations.c.id == self.operation_id).values(
                next_attempt_at='2000-01-01T00:00:00+00:00'))
        self.assertEqual(self.run_worker()['status'], 'COMPLETED')
        self.assertEqual(self.operation()['attempt_count'], 2)
        self.assertEqual(self.receipt_count(), 1)

    def test_stale_finalizer_cannot_ack_until_expired_lease_recovery(self):
        original = monitor.refresh_fixture_source
        def committed_then_lease_expired(*args, **kwargs):
            result = original(*args, **kwargs)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == self.operation_id).values(
                    lease_expires_at='2000-01-01T00:00:00+00:00'))
            return result
        with patch.object(monitor, 'refresh_fixture_source', committed_then_lease_expired):
            self.assertEqual(self.run_worker()['status'], 'OWNERSHIP_EXPIRED')
        self.assertEqual(self.operation()['state'], 'RUNNING')
        self.expire_and_reopen()
        self.assertEqual(self.operation()['state'], 'COMPLETED')
        self.assertEqual(self.receipt_count(), 1)


if __name__ == '__main__':
    unittest.main()
