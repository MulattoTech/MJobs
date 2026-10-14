"""Committed imports survive cancellation, acknowledgement failure and process death."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.monitoring import refresh_company_source, schedule_company_sources
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.company_feed import import_company_feed_jobs
from tests.integration.test_company_monitoring_recovery import MonitoringFixture
from workers.operations import MAX_ATTEMPTS, OperationTimedOut, claim, recover_expired, request_cancel
from workers.tasks import execute_operation


class CompanyRefreshReceiptTests(unittest.TestCase):
    def make_fixture(self):
        return MonitoringFixture()

    def setUp(self):
        self.fixture = self.make_fixture()
        self.addCleanup(self.fixture.close)
        self.store = self.fixture.store
        self.fixture.configure()
        self.operation_id = schedule_company_sources(self.store)['scheduled'][0]['operation_id']

    def row(self):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.operation_id)).mappings().one())

    def change(self, **values):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(**values))

    def execute_after_import(self, callback):
        def wrapped(*args, **kwargs):
            result = import_company_feed_jobs(*args, **kwargs)
            callback()
            return result
        with patch.dict(os.environ, self.fixture.env), patch.object(
                PublicCompanyClient, '_request_once', self.fixture.fetch), patch(
                'services.discovery.company_feed.import_company_feed_jobs', side_effect=wrapped):
            return execute_operation(self.operation_id)

    def assert_completed(self):
        row = self.row()
        self.assertEqual(row['state'], 'COMPLETED')
        self.assertEqual(row['progress'], 100)
        self.assertIsNone(row['error'])
        self.assertIsNone(row['lease_expires_at'])
        self.assertIsNotNone(row['completed_at'])
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.fixture.root, redis_url='redis://127.0.0.1:1/0')) as client:
            response = client.get(f'/operations/{self.operation_id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['state'], 'COMPLETED')

    def test_cancel_after_real_import_cannot_erase_committed_outcome(self):
        responses = []
        def cancel():
            with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.fixture.root,
                                       redis_url='redis://127.0.0.1:1/0')) as client:
                responses.append(client.post(f'/operations/{self.operation_id}/cancel'))
        result = self.execute_after_import(cancel)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].status_code, 202)
        self.assertEqual(responses[0].json().get('reason'), 'REFRESH_ALREADY_SAVED')
        self.assertFalse(responses[0].json()['cancel_requested'])
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assert_completed()
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        self.assertEqual(self.store.count('source_quality_observations'), 1)
        with self.store.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(m.audit.c.id).where(
                m.audit.c.entity_id == self.operation_id,
                m.audit.c.action == 'CANCELLATION_REQUESTED')).first())

    def assert_saved_import_wins(self, failure):
        def fail_after_refresh(*args, **kwargs):
            refresh_company_source(*args, **kwargs)
            raise failure
        # Exercise the worker acknowledgement boundary, after the service has
        # finished its transaction and monitor bookkeeping.
        with patch.dict(os.environ, self.fixture.env), patch.object(
                PublicCompanyClient, '_request_once', self.fixture.fetch), patch(
                'services.company_discovery.monitoring.refresh_company_source', side_effect=fail_after_refresh):
            result = execute_operation(self.operation_id)
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assert_completed()
        monitor = self.fixture.monitor()
        self.assertEqual(monitor['status'], 'HEALTHY')
        self.assertEqual(monitor['failure_count'], 0)
        self.assertIsNone(monitor['last_error'])
        with self.store.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(m.audit.c.id).where(
                m.audit.c.action == 'COMPANY_SOURCE_REFRESH_FAILED')).first())

    def test_saved_import_wins_post_commit_timeout(self):
        self.assert_saved_import_wins(OperationTimedOut('OPERATION_TIMEOUT'))

    def test_saved_import_wins_post_commit_exception(self):
        self.assert_saved_import_wins(RuntimeError('ack failed'))

    def test_receipt_does_not_override_newer_worker_attempt(self):
        def supersede():
            self.change(attempt_count=self.row()['attempt_count'] + 1)
        result = self.execute_after_import(supersede)
        self.assertEqual(result['status'], 'OWNERSHIP_EXPIRED')
        self.assertEqual(self.row()['state'], 'RUNNING')
        self.assertEqual(self.row()['attempt_count'], 2)

    def test_cancel_waits_for_import_transaction_receipt(self):
        receipt_written, release_commit, cancel_started = Event(), Event(), Event()
        original_audit = Store._audit
        def hold_receipt(store, connection, entity, action, payload):
            original_audit(store, connection, entity, action, payload)
            if action == 'COMPANY_SOURCE_REFRESHED' and payload.get('operation_id') == self.operation_id:
                receipt_written.set()
                if not release_commit.wait(10):
                    raise RuntimeError('test did not release import')
        def cancel():
            cancel_started.set()
            return request_cancel(self.store, self.operation_id)
        with patch.dict(os.environ, self.fixture.env), patch.object(
                PublicCompanyClient, '_request_once', self.fixture.fetch), patch.object(Store, '_audit', hold_receipt):
            with ThreadPoolExecutor(max_workers=2) as pool:
                worker = pool.submit(execute_operation, self.operation_id)
                try:
                    self.assertTrue(receipt_written.wait(10))
                    cancellation = pool.submit(cancel)
                    self.assertTrue(cancel_started.wait(2))
                    self.assertFalse(cancellation.done())
                finally:
                    release_commit.set()
                response = cancellation.result(timeout=10)
                result = worker.result(timeout=10)
        # Either cancellation sees the receipt before acknowledgement, or the
        # worker has already completed. Neither path requests cancellation.
        self.assertIn(response.get('reason'), {'REFRESH_ALREADY_SAVED', 'TERMINAL_OPERATION'})
        self.assertFalse(response['cancel_requested'])
        self.assertEqual(result['status'], 'COMPLETED')
        self.assert_completed()

    def test_receipt_survives_monitor_pointer_change(self):
        cancellations = []
        def replace_pointer_then_cancel():
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_source_monitoring).values(completed_operation_id=None))
            cancellations.append(request_cancel(self.store, self.operation_id))
        result = self.execute_after_import(replace_pointer_then_cancel)
        self.assertEqual(cancellations[0].get('reason'), 'REFRESH_ALREADY_SAVED')
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assert_completed()

    def test_unrelated_receipts_never_prevent_cancellation(self):
        source = self.fixture.source['id']
        variants = [
            ('OTHER_ACTION', source, source, self.operation_id),
            ('COMPANY_SOURCE_REFRESHED', 'other-source', source, self.operation_id),
            ('COMPANY_SOURCE_REFRESHED', source, 'other-source', self.operation_id),
            ('COMPANY_SOURCE_REFRESHED', source, source, 'other-operation'),
        ]
        for action, entity, payload_source, payload_operation in variants:
            with self.subTest(action=action, entity=entity, source=payload_source, operation=payload_operation):
                self.change(state='QUEUED', cancel_requested=False)
                with self.store.engine.begin() as connection:
                    self.store._audit(connection, entity, action, dict(
                        company_source_id=payload_source, operation_id=payload_operation))
                self.change(state='RUNNING', attempt_count=1,
                            lease_expires_at='2000-01-01T00:00:00+00:00')
                self.assertEqual(recover_expired(self.store), 1)
                self.assertEqual(self.row()['state'], 'QUEUED')
                self.assertEqual(request_cancel(self.store, self.operation_id)['state'], 'CANCELLED')
        self.assertEqual(self.store.count('canonical_jobs'), 0)

    def test_kind_scope_and_missing_receipt_preserve_normal_recovery(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.change(lease_expires_at='2000-01-01T00:00:00+00:00')
        self.assertEqual(recover_expired(self.store), 1)
        self.assertEqual(self.row()['state'], 'QUEUED')
        with self.store.engine.begin() as connection:
            self.store._audit(connection, self.fixture.source['id'], 'COMPANY_SOURCE_REFRESHED', dict(
                company_source_id=self.fixture.source['id'], operation_id=self.operation_id))
        self.change(kind='COMPANY_DISCOVER')
        self.assertEqual(request_cancel(self.store, self.operation_id)['state'], 'CANCELLED')

    def test_process_death_after_commit_recovers_without_network_or_duplicate_import(self):
        pages = self.fixture.root / 'pages.json'
        pages.write_text(json.dumps(self.fixture.pages), encoding='utf-8')
        requests = self.fixture.root / 'requests.txt'
        script = '''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
import httpx
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.company_feed import import_company_feed_jobs as original
from workers.tasks import execute_operation
pages = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
def fetch(client, url, deadline):
    request = httpx.Request('GET', url)
    with open(sys.argv[3], 'a', encoding='utf-8') as output:
        output.write(request.url.path + '\\n')
    return httpx.Response(200, text=pages[request.url.path], request=request)
def crash(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(73)
with patch.object(PublicCompanyClient, '_request_once', fetch), patch(
        'services.discovery.company_feed.import_company_feed_jobs', side_effect=crash):
    execute_operation(sys.argv[1])
'''
        result = subprocess.run([sys.executable, '-c', script, self.operation_id, str(pages), str(requests)],
                                cwd=Path(__file__).resolve().parents[2],
                                env={**os.environ, **self.fixture.env}, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(self.row()['state'], 'RUNNING')
        self.assertEqual(self.row()['attempt_count'], 1)
        self.assertEqual(self.fixture.monitor()['completed_operation_id'], self.operation_id)
        tables = ('canonical_jobs', 'job_postings', 'job_matches', 'source_quality_observations')
        counts = {table: self.store.count(table) for table in tables}
        self.assertEqual(counts['canonical_jobs'], 1)
        network_before = requests.read_bytes()
        self.store.engine.dispose()
        self.store = Store(self.fixture.store.url)
        self.addCleanup(self.store.engine.dispose)
        # Even a spent attempt budget and old cancellation flag cannot erase a
        # transaction that really committed before the process died.
        self.change(lease_expires_at='2000-01-01T00:00:00+00:00',
                    attempt_count=MAX_ATTEMPTS, cancel_requested=True)
        with patch.object(PublicCompanyClient, 'get', side_effect=AssertionError('unexpected egress')):
            self.assertEqual(recover_expired(self.store), 1)
            self.assert_completed()
            with patch.dict(os.environ, self.fixture.env):
                self.assertEqual(execute_operation(self.operation_id)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual({table: self.store.count(table) for table in tables}, counts)
        self.assertEqual(requests.read_bytes(), network_before)
        with self.store.engine.connect() as connection:
            for action in ('COMPANY_SOURCE_REFRESHED', 'COMPANY_FEED_IMPORT_COMPLETE'):
                self.assertEqual(len(connection.execute(select(m.audit.c.id).where(m.audit.c.action == action)).all()), 1)


@unittest.skipUnless(os.environ.get('MJOBS_REFRESH_POSTGRES_URL'), 'Requires isolated refresh PostgreSQL')
class PostgreSQLCompanyRefreshReceiptTests(CompanyRefreshReceiptTests):
    @classmethod
    def setUpClass(cls):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.engine import make_url
        cls.database_url = os.environ['MJOBS_REFRESH_POSTGRES_URL']
        target = make_url(cls.database_url)
        if target.database != 'mjobs_refresh_validation' or target.host not in {'127.0.0.1', 'localhost'}:
            raise ValueError('Requires disposable loopback database mjobs_refresh_validation')
        with patch.dict(os.environ, MJOBS_DATABASE_URL=cls.database_url):
            command.upgrade(Config(str(Path(__file__).resolve().parents[2] / 'alembic.ini')), 'head')

    def make_fixture(self):
        from sqlalchemy import text
        store = Store(self.database_url)
        # The isolated database guard above is mandatory before any truncation.
        with store.engine.begin() as connection:
            connection.execute(text('TRUNCATE ' + ', '.join(
                '"' + table.name + '"' for table in m.metadata.sorted_tables) + ' CASCADE'))
        store.migrate = lambda: None  # Already migrated by Alembic above.
        with patch('tests.integration.test_company_monitoring_recovery.Store', return_value=store):
            return MonitoringFixture()


if __name__ == '__main__':
    unittest.main()
