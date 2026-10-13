"""Direct employer discovery intents coalesce without losing durable retry identity."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.triggers import queue_company_discovery
from services.company_discovery.domains import record_company_domain
from tests.integration.test_spine import PROFILE


class CompanyDiscoveryIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'discovery.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(PROFILE)
        self.store.save_profile({**deepcopy(PROFILE), 'id': 'other', 'name': 'Other'})
        self.candidate = {'company_name': 'Synthetic Employer',
                          'company_website': 'https://employer.example/'}

    def queue(self, request_id, **options):
        return queue_company_discovery(self.store, options.pop('candidate', self.candidate),
            options.pop('reason', 'USER_ADDED_COMPANY'), options.pop('profile_id', 'fixture'),
            public=True, request_id=request_id, **options)

    def test_new_tab_intents_share_one_active_operation_and_survive_reopen(self):
        first = self.queue('first-tab')
        second = self.queue('second-tab')
        self.assertEqual(first['operation_id'], second['operation_id'])
        self.assertEqual(self.store.count('worker_operations'), 1)
        reopened = Store(self.store.url)
        try:
            replay = queue_company_discovery(reopened, self.candidate, 'USER_ADDED_COMPANY',
                'fixture', public=True, request_id='second-tab')
            self.assertEqual(replay['operation_id'], first['operation_id'])
        finally:
            reopened.engine.dispose()

    def test_concurrent_intents_share_one_operation(self):
        barrier = threading.Barrier(6)
        def enqueue(index):
            barrier.wait(timeout=10)
            return self.queue(f'tab-{index}')['operation_id']
        with ThreadPoolExecutor(max_workers=6) as executor:
            ids = list(executor.map(enqueue, range(6)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.store.count('worker_operations'), 1)

    def test_running_operation_is_coalesced(self):
        first = self.queue('first')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).values(state='RUNNING').where(
                m.operations.c.id == first['operation_id']))
        self.assertEqual(self.queue('second')['operation_id'], first['operation_id'])

    def test_candidate_mapping_order_does_not_change_intent_identity(self):
        first = self.queue('first')
        reordered = dict(reversed(list(self.candidate.items())))
        self.assertEqual(self.queue('first', candidate=reordered)['operation_id'], first['operation_id'])
        self.assertEqual(self.queue('second', candidate=reordered)['operation_id'], first['operation_id'])

    def test_terminal_failure_allows_fresh_intent_but_replays_both_original_intents(self):
        first = self.queue('first')
        second = self.queue('second')
        self.assertEqual(first['operation_id'], second['operation_id'])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).values(state='DEAD_LETTERED').where(
                m.operations.c.id == first['operation_id']))
        self.assertEqual(self.queue('first')['operation_id'], first['operation_id'])
        self.assertEqual(self.queue('second')['operation_id'], first['operation_id'])
        fresh = self.queue('fresh-recovery')
        self.assertNotEqual(fresh['operation_id'], first['operation_id'])
        self.assertEqual(self.store.count('worker_operations'), 2)

    def test_different_profile_candidate_and_trigger_authority_are_separate(self):
        first = self.queue('first')
        variations = [dict(profile_id='other'),
            dict(candidate={**self.candidate, 'company_website': 'https://different.example/'}),
            dict(candidate={**self.candidate, 'company_website': 'https://employer.example/division'}),
            dict(candidate={**self.candidate, 'application_url': 'https://employer.example/jobs/1'}),
            dict(reason='KNOWN_SOURCE_BROKEN')]
        for index, variation in enumerate(variations):
            with self.subTest(variation=variation):
                self.assertNotEqual(self.queue(f'intent-{index}', **variation)['operation_id'],
                                    first['operation_id'])

    def test_changed_profile_requires_new_scoring_snapshot_original_intent_replays(self):
        first = self.queue('first')
        profile = deepcopy(PROFILE)
        profile['name'] = 'Changed Profile'
        self.store.save_profile(profile)
        self.assertEqual(self.queue('first')['operation_id'], first['operation_id'])
        self.assertNotEqual(self.queue('changed-profile')['operation_id'], first['operation_id'])
        with self.store.engine.connect() as connection:
            rows = list(connection.execute(select(m.operations)).mappings())
        self.assertEqual(len({row['params']['profile_version'] for row in rows}), 2)

    def test_new_verified_company_identity_cannot_reuse_previous_company_operation(self):
        first = self.queue('unverified')
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(id='verified-owner', name='Verified Owner', domain=''))
        record_company_domain(self.store, 'verified-owner', 'employer.example',
            'FIRST_PARTY_JOB_LINK', ['Controlled first-party job evidence'])
        verified = self.queue('verified')
        self.assertEqual(verified['company_id'], 'verified-owner')
        self.assertNotEqual(verified['operation_id'], first['operation_id'])
        self.assertEqual(self.queue('unverified')['operation_id'], first['operation_id'])

    def test_review_and_monitoring_authority_never_share_direct_add_operation(self):
        first = self.queue('direct')
        with self.store.engine.begin() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == first['operation_id'])).mappings().one())
            params = dict(operation['params'])
            params['discovery_request_id'] = 'review-authority'
            params['discovery_request_revision'] = 7
            connection.execute(update(m.operations).where(m.operations.c.id == first['operation_id'])
                               .values(params=params))
        second = self.queue('new-direct')
        self.assertNotEqual(second['operation_id'], first['operation_id'])
        with self.store.engine.begin() as connection:
            params = dict(connection.execute(select(m.operations.c.params).where(
                m.operations.c.id == second['operation_id'])).scalar_one())
            params['monitoring_source_id'] = 'monitoring-authority'
            connection.execute(update(m.operations).where(m.operations.c.id == second['operation_id'])
                               .values(params=params))
        third = self.queue('third-direct')
        self.assertNotIn(third['operation_id'], [first['operation_id'], second['operation_id']])

    def test_disposable_postgres_guard_rejects_connection_overrides(self):
        for query in ['?hostaddr=203.0.113.5', '?service=unsafe', '?options=-csearch_path=private']:
            with self.subTest(query=query), self.assertRaisesRegex(RuntimeError, 'Disposable loopback'):
                _disposable_postgres_url('postgresql://test:test@localhost/mjobs_test' + query)

    def test_request_payload_conflict_never_coalesces_or_enqueues(self):
        self.queue('same-intent')
        with self.assertRaisesRegex(ValueError, 'COMPANY_DISCOVERY_REQUEST_CONFLICT'):
            self.queue('same-intent', candidate={**self.candidate,
                'company_website': 'https://other.example/'})
        self.assertEqual(self.store.count('worker_operations'), 1)

    def test_receipt_failure_rolls_back_outbox_then_intent_can_recover(self):
        from sqlalchemy.engine import Connection
        original = Connection.execute
        def fail_receipt(connection, statement, *args, **kwargs):
            if getattr(statement, 'is_insert', False) and statement.table is m.audit:
                raise RuntimeError('Synthetic receipt interruption')
            return original(connection, statement, *args, **kwargs)
        with patch.object(Connection, 'execute', fail_receipt):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic receipt interruption'):
                self.queue('interrupted')
        self.assertEqual(self.store.count('worker_operations'), 0)
        recovered = self.queue('interrupted')
        self.assertEqual(self.queue('interrupted')['operation_id'], recovered['operation_id'])
        self.assertEqual(self.store.count('worker_operations'), 1)

    def test_api_new_tab_retry_returns_original_operation_and_conflict_is_409(self):
        app = create_app(self.store, 'http://127.0.0.1:18799', Path(self.temp.name) / 'outputs',
                         redis_url='redis://127.0.0.1:1/0', operation_mode='PERSONAL')
        body = {**self.candidate, 'reason': 'USER_ADDED_COMPANY', 'profile_id': 'fixture'}
        with TestClient(app) as client:
            first = client.post('/companies/discovery', json={**body, 'request_id': 'first'})
            second = client.post('/companies/discovery', json={**body, 'request_id': 'second'})
            self.assertEqual(first.status_code, 202, first.text)
            self.assertEqual(second.status_code, 202, second.text)
            self.assertEqual(first.json()['operation_id'], second.json()['operation_id'])
            conflict = client.post('/companies/discovery', json={**body,
                'company_website': 'https://different.example/', 'request_id': 'second'})
            self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_profile_activity_and_dead_letters_retain_company_operation_without_cross_profile_leak(self):
        own = self.queue('own')
        other = self.queue('other', profile_id='other')
        app = create_app(self.store, 'http://127.0.0.1:18799', Path(self.temp.name) / 'outputs',
                         redis_url='redis://127.0.0.1:1/0', operation_mode='PERSONAL')
        with TestClient(app) as client:
            rows = client.get('/operations?profile_id=fixture').json()['operations']
            self.assertEqual([row['id'] for row in rows], [own['operation_id']])
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).values(state='DEAD_LETTERED'))
            rows = client.get('/operations/dead-letter-queue?profile_id=fixture').json()['operations']
            self.assertEqual([row['id'] for row in rows], [own['operation_id']])
            rows = client.get('/operations/dead-letter-queue?profile_id=other').json()['operations']
            self.assertEqual([row['id'] for row in rows], [other['operation_id']])


def _disposable_postgres_url(value):
    url = make_url(value)
    if (not url.drivername.startswith('postgresql') or
            url.host not in ('localhost', '127.0.0.1', '::1') or url.query or
            not url.database or not url.database.startswith('mjobs_test')):
        raise RuntimeError('Disposable loopback mjobs_test database without URL overrides required')
    return url


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'),
                     'Disposable PostgreSQL URL is required')
class CompanyDiscoveryIdempotencyPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = _disposable_postgres_url(os.environ['MJOBS_TEST_POSTGRES_URL'])
        self.schema = 'company_intent_' + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        scoped = url.update_query_dict({'options': '-csearch_path=' + self.schema +
            ' -clock_timeout=10000 -cstatement_timeout=30000'})
        database_url = scoped.render_as_string(hide_password=False)
        class PostgreSQLStore(Store):
            def __init__(self, _ignored):
                super().__init__(database_url)
            def migrate(self):
                m.metadata.create_all(self.engine)
        self.case = CompanyDiscoveryIdempotencyTests()
        with patch(f'{__name__}.Store', PostgreSQLStore):
            self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_concurrent_postgresql_intents(self):
        self.case.test_concurrent_intents_share_one_operation()

    def test_postgresql_terminal_replay_and_fresh_retry(self):
        self.case.test_terminal_failure_allows_fresh_intent_but_replays_both_original_intents()

    def test_postgresql_profile_and_candidate_isolation(self):
        self.case.test_different_profile_candidate_and_trigger_authority_are_separate()
