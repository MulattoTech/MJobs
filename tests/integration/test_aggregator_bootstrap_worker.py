"""Staged aggregator→native bootstrap through the actual company worker."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import event, select, text, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.aggregator_bootstrap import BudgetedPublicClient, canonical_digest
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.sources.greenhouse import Greenhouse
from tests.integration.test_official_domain_canonical_apply_proof import _apply_page, _job_page
from workers.tasks import execute_operation


class _ControlledV2Rollback(RuntimeError):
    """Deliberate test-only rollback after a real guarded row-lock acquisition."""


class _ProfileTransactionRace:
    """Observe actual PostgreSQL locks; events only choose their ordering.

    Neither SQL, a guard result nor Store.save_profile is replaced. The writer's
    first real CAS UPDATE is held *after* PostgreSQL has executed it. A second
    connection can therefore prove that SHARE waits/skip behavior belongs to the
    database, rather than infer it from elapsed wall time or a paused thread.
    """

    def __init__(self, case, writer, ordering, *, rollback=False):
        self.case = case
        self.writer = writer
        self.ordering = ordering
        self.rollback = rollback
        self.share_seen = Event()
        self.release_share = Event()
        self.writer_update_started = Event()
        self.writer_locked = Event()
        self.release_writer = Event()
        self.stage_done = Event()
        self.writer_done = Event()
        self.stage_result = {}
        self.writer_result = {}
        self.stage_pid = None
        self.writer_pid = None
        self.share_rows = None
        self.share_sql = None
        self.capture_stage = False
        self.threads = []

    @staticmethod
    def _profile_update(context):
        statement = getattr(getattr(context, 'compiled', None), 'statement', None)
        return (getattr(statement, 'is_update', False)
                and getattr(getattr(statement, 'table', None), 'name', None) == m.profiles.name)

    def _after_stage_sql(self, connection, cursor, statement, parameters, context, executemany):
        normalized = ' '.join(statement.replace('"', '').upper().split())
        if (self.share_seen.is_set() or 'FOR SHARE SKIP LOCKED' not in normalized
                or 'FROM ' + m.profiles.name.upper() not in normalized):
            return
        self.share_rows = cursor.rowcount
        self.share_sql = normalized
        self.stage_pid = connection.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()
        self.share_seen.set()
        if self.ordering == 'share_first':
            if self.share_rows != 1:
                raise AssertionError('The actual profile SHARE query did not return the pinned row')
            if not self.release_share.wait(10):
                raise AssertionError('The test coordinator did not release the stage transaction')
            if self.rollback:
                raise _ControlledV2Rollback('controlled rollback after real profile SHARE')

    def _before_stage_sql(self, connection, cursor, statement, parameters, context, executemany):
        if not self.capture_stage or self.stage_pid is not None:
            return
        self.stage_pid = 0  # prevent re-entry for the real backend-ID query
        self.stage_pid = connection.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()

    def call_stage(self, call):
        self.capture_stage = True
        return call()

    def _before_writer_sql(self, connection, cursor, statement, parameters, context, executemany):
        if self.writer_update_started.is_set() or not self._profile_update(context):
            return
        self.writer_pid = connection.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()
        self.writer_update_started.set()

    def _after_writer_sql(self, connection, cursor, statement, parameters, context, executemany):
        if self.writer_locked.is_set() or not self._profile_update(context):
            return
        if cursor.rowcount != 1:
            raise AssertionError('The normal profile CAS UPDATE did not lock one owned row')
        self.writer_locked.set()
        if not self.release_writer.wait(10):
            raise AssertionError('The test coordinator did not release Store.save_profile')

    def __enter__(self):
        event.listen(self.case.store.engine, 'before_cursor_execute', self._before_stage_sql)
        event.listen(self.case.store.engine, 'after_cursor_execute', self._after_stage_sql)
        event.listen(self.writer.engine, 'before_cursor_execute', self._before_writer_sql)
        event.listen(self.writer.engine, 'after_cursor_execute', self._after_writer_sql)
        return self

    def start(self, target, result, done, *, name):
        def run():
            try:
                result['value'] = target()
            except BaseException as error:
                result['error'] = error
            finally:
                done.set()
        thread = Thread(target=run, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()

    def database_blocking_observation(self):
        """Wait for a real lock edge, bounded by DB timeouts and a fixed probe cap."""
        deadline = monotonic() + 2.0
        with self.case.store.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as observer:
            # This is the isolated test schema's third connection, not either actor.
            old_timeout = observer.exec_driver_sql('SHOW statement_timeout').scalar_one()
            observer.exec_driver_sql("SET statement_timeout = '1000ms'")
            try:
                for _ in range(512):
                    row = dict(observer.execute(text(
                        'SELECT pg_backend_pid() AS observer_pid, '
                        'pg_blocking_pids(:writer_pid) AS blockers, '
                        '(SELECT wait_event_type FROM pg_stat_activity WHERE pid = :writer_pid) AS wait_type'
                    ), {'writer_pid': self.writer_pid}).mappings().one())
                    if self.stage_pid in row['blockers'] and row['wait_type'] == 'Lock':
                        return row
                    if self.writer_locked.is_set() or self.writer_done.is_set() or monotonic() >= deadline:
                        break
            finally:
                observer.execute(text("SELECT set_config('statement_timeout', :value, false)"),
                                 {'value': old_timeout})
        raise AssertionError('PostgreSQL did not show the profile writer blocked by the real stage SHARE')

    def __exit__(self, exc_type, exc, traceback):
        self.release_share.set()
        self.release_writer.set()
        for thread in self.threads:
            thread.join(timeout=10)
        event.remove(self.case.store.engine, 'before_cursor_execute', self._before_stage_sql)
        event.remove(self.case.store.engine, 'after_cursor_execute', self._after_stage_sql)
        event.remove(self.writer.engine, 'before_cursor_execute', self._before_writer_sql)
        event.remove(self.writer.engine, 'after_cursor_execute', self._after_writer_sql)
        if any(thread.is_alive() for thread in self.threads):
            raise AssertionError('A bounded PostgreSQL race actor did not exit before fixture cleanup')


class AggregatorBootstrapWorkerTests(unittest.TestCase):
    def setUp(self):
        from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests

        self.case = AggregatorBootstrapCollectionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.case.tearDown)
        output = tempfile.TemporaryDirectory(prefix='mjobs-bootstrap-worker-output-')
        self.addCleanup(output.cleanup)
        self.output_dir = Path(output.name)
        raw = self.case.rows[0]
        self.title = raw['title']
        self.company = raw['company_name']
        raw['description'] += (
            '<p><a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply to this job</a></p>'
        )
        self.raw_before = deepcopy(raw)
        # Worker effect tests use an explicitly authorized, real V2 lineage.
        # Legacy V1 behavior is isolated in its own compatibility regression.
        profile = deepcopy(self.case.store.profile('fixture'))
        profile['preferences']['company_discovery']['employer_verification'] = {
            'version': 1, 'mode': 'GREENHOUSE_RECIPROCAL_V1', 'activation_id': None,
        }
        self.case.store.save_profile(profile, expected_version=profile['version'], require_expected_version=True)
        self.parent_lineage = self.case.parent_operation()
        self.collection = self.case.collect(**self.parent_lineage)
        with self.case.store.engine.connect() as connection:
            self.request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            self.operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.request['operation_id'])).mappings().one())
        self.job_url = 'https://acme.example/careers/listing/senior-python-engineer/1001'
        self.apply_url = 'https://acme.example/careers/apply/senior-python-engineer/1001'
        self.job_html = _job_page(
            canonical=self.job_url, job_url=self.job_url, job_id='1001',
            title=self.title, company=self.company, apply_href=self.apply_url
        ).replace('https://employer.example', 'https://acme.example')
        self.apply_html = _apply_page(iframe_src=(
            'https://job-boards.greenhouse.io/embed/job_app?for=acme&token=1001'))
        self.native_job = {
            'id': 1001, 'title': self.title, 'absolute_url': self.job_url,
            'content': '<p>Build Python SQL backend services.</p>',
        }
        self.network = []
        self.saw_unconfigured_source_before_first_fetch = None

    def test_queued_v1_without_employer_verification_consent_blocks_before_egress(self):
        from services.company_discovery.aggregator_bootstrap import canonical_digest

        # Reconstruct a valid historical V1 queue from the real V2 family,
        # then model a legacy profile whose current consent is disabled.
        pending = deepcopy(self.request['domain_evidence'][0])
        pending['schema'] = 'AGGREGATOR_BOOTSTRAP_PENDING_V1'
        pending['version'] = 1
        for key in ('parent_request_id', 'verification_policy', 'verification_policy_sha256'):
            pending['binding'].pop(key)
        pending['sha256'] = canonical_digest({key: value for key, value in pending.items() if key != 'sha256'})
        profile = self.case.store.profile('fixture')
        preferences = deepcopy(profile['preferences'])
        preferences['company_discovery']['employer_verification'] = {
            'version': 1, 'mode': 'DISABLED', 'activation_id': None,
        }
        with self.case.store.engine.begin() as connection:
            profile_version = connection.execute(select(m.profile_versions).where(
                m.profile_versions.c.profile_id == 'fixture',
                m.profile_versions.c.version == profile['version'])).mappings().one()
            contact = deepcopy(profile_version['contact'])
            contact['preferences'] = preferences
            connection.execute(update(m.profile_versions).where(
                m.profile_versions.c.id == profile_version['id']).values(contact=contact))
            connection.execute(update(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request['id']).values(domain_evidence=[pending]))
            params = deepcopy(self.operation['params'])
            params['aggregator_bootstrap'] = 'AGGREGATOR_BOOTSTRAP_PENDING_V1'
            params['aggregator_bootstrap_version'] = 1
            params['fixture_aggregator_bootstrap'] = 'fixture-aggregator-bootstrap-v1'
            params['aggregator_bootstrap_pending_sha256'] = pending['sha256']
            connection.execute(update(m.operations).where(m.operations.c.id == self.operation['id']).values(
                params=params))
        self.request['domain_evidence'] = [pending]
        self.operation['params'] = params

        outcome = self._execute()

        self.assertEqual(outcome["status"], "BLOCKED", outcome)
        self.assertEqual(self.network, [])
        self.assertEqual(self.case.store.count("company_domains"), 0)
        with self.case.store.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(m.company_sources).where(
                m.company_sources.c.provider == "greenhouse")).mappings()), [])
        from services.company_discovery.public_client import PublicFetchError
        renewed_profile = self.case.store.profile('fixture')
        renewed_profile['preferences']['company_discovery']['employer_verification'] = {
            'version': 1, 'mode': 'GREENHOUSE_RECIPROCAL_V1', 'activation_id': None,
        }
        self.case.store.save_profile(renewed_profile, expected_version=renewed_profile['version'],
                                     require_expected_version=True)
        from services.company_discovery.requests import authorize_discovery_request
        with self.assertRaises(PublicFetchError):
            authorize_discovery_request(self.case.store, self.operation)

    def test_personal_no_hint_job_cannot_replace_queued_typed_request(self):
        from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests

        case = AggregatorBootstrapCollectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        case._activate_v2()
        with case.store.engine.begin() as connection:
            connection.execute(update(m.workspace_identity).where(
                m.workspace_identity.c.id == 'workspace').values(operation_mode='PERSONAL'))
        first = case.rows[0]
        first['description'] += '<p><a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a></p>'
        case.collect(**case.parent_operation())
        before = case.pending()[0]
        with case.store.engine.connect() as connection:
            before_operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == before['operation_id'])).mappings().one())

        second = deepcopy(first)
        second.update(id=1002, title='Senior Python Engineer II',
            url='https://remotive.com/remote-jobs/software-dev/senior-python-engineer-1002')
        second['description'] = 'Build Python and SQL services for the Acme platform.'
        case.rows = [second]
        with case.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == case.source_id).values(last_attempt=None))
        case.collect(**case.parent_operation())
        after = case.pending()[0]
        with case.store.engine.connect() as connection:
            after_operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == after['operation_id'])).mappings().one())

        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['candidate_id'], before['candidate_id'])
        self.assertEqual(after['trigger_job_id'], before['trigger_job_id'])
        self.assertEqual(after['authorization'], 'AGGREGATOR_BOOTSTRAP_PENDING')
        self.assertEqual(after['operation_id'], before['operation_id'])
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(after['domain_evidence'], before['domain_evidence'])
        self.assertEqual(after_operation['params']['aggregator_bootstrap'],
            before_operation['params']['aggregator_bootstrap'])
        self.assertEqual(after_operation['params']['aggregator_bootstrap_pending_sha256'],
            before_operation['params']['aggregator_bootstrap_pending_sha256'])

    def test_parent_collection_attempt_is_bound_while_running_and_survives_completion(self):
        from services.company_discovery.requests import _aggregator_bootstrap_pending
        from workers.dispatch import enqueue_operation

        with self.case.store.engine.begin() as connection:
            parent_id = enqueue_operation(self.case.store, 'DISCOVER', 'fixture', {
                'source_id': self.case.source_id, 'profile_ids': ['fixture'],
                'profile_version': self.case.store.profile('fixture')['version'],
                'request_id': 'parent-lineage-test'},
                connection=connection, task_key='aggregator-parent-lineage-test')
            connection.execute(update(m.operations).where(m.operations.c.id == parent_id).values(
                state='RUNNING', attempt_count=1, cancel_requested=False,
                lease_expires_at='2099-01-01T00:00:00+00:00'))
            request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            hint = request['domain_evidence'][0]['hint']
            staged = _aggregator_bootstrap_pending(connection, request, hint, fixture=True,
                target_revision=request['revision'], parent_operation_id=parent_id,
                parent_operation_attempt=1, validate_parent_staging=True)
            self.assertIsNotNone(staged)
            self.assertEqual(staged['binding']['parent_operation_id'], parent_id)
            self.assertEqual(staged['binding']['parent_operation_attempt'], 1)
            connection.execute(update(m.operations).where(m.operations.c.id == parent_id).values(
                cancel_requested=True))
            self.assertIsNone(_aggregator_bootstrap_pending(connection, request, hint, fixture=True,
                target_revision=request['revision'], parent_operation_id=parent_id,
                parent_operation_attempt=1))
            connection.execute(update(m.operations).where(m.operations.c.id == parent_id).values(
                cancel_requested=False, lease_expires_at='2000-01-01T00:00:00+00:00'))
            self.assertIsNone(_aggregator_bootstrap_pending(connection, request, hint, fixture=True,
                target_revision=request['revision'], parent_operation_id=parent_id,
                parent_operation_attempt=1))
            connection.execute(update(m.operations).where(m.operations.c.id == parent_id).values(
                lease_expires_at='2099-01-01T00:00:00+00:00'))
            connection.execute(update(m.operations).where(m.operations.c.id == parent_id).values(
                state='COMPLETED'))
            after_completion = _aggregator_bootstrap_pending(connection, request, hint, fixture=True,
                target_revision=request['revision'], parent_operation_id=parent_id,
                parent_operation_attempt=1)
            self.assertEqual(after_completion, staged)
            connection.execute(update(m.operations).where(m.operations.c.id == parent_id).values(
                params={'source_id': 'another-source', 'profile_ids': ['fixture']}))
            self.assertIsNone(_aggregator_bootstrap_pending(connection, request, hint, fixture=True,
                target_revision=request['revision'], parent_operation_id=parent_id,
                parent_operation_attempt=1))

    def test_parent_lineage_survives_actual_collection_reopen_and_child_authorization(self):
        from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests
        from services.company_discovery.requests import authorize_discovery_request

        case = AggregatorBootstrapCollectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        case._activate_v2()
        lineage = case.parent_operation()
        case.collect(**lineage)
        pending = case.pending()[0]
        intent = pending['domain_evidence'][0]
        self.assertEqual(intent['binding']['parent_operation_id'], lineage['parent_operation_id'])
        self.assertEqual(intent['binding']['parent_operation_attempt'], lineage['parent_operation_attempt'])
        reopened = Store(case.url)
        self.addCleanup(reopened.engine.dispose)
        with reopened.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == lineage['parent_operation_id']).values(state='COMPLETED'))
        with reopened.engine.connect() as connection:
            child_operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == pending['operation_id'])).mappings().one())
            parent_operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == lineage['parent_operation_id'])).mappings().one())
        self.assertEqual(parent_operation['attempt_count'], lineage['parent_operation_attempt'])
        authorized = authorize_discovery_request(reopened, child_operation)
        self.assertEqual(authorized['authorization'], 'AGGREGATOR_BOOTSTRAP_PENDING')
        self.assertEqual(authorized['aggregator_bootstrap_pending']['binding']['parent_operation_id'],
            lineage['parent_operation_id'])

    def test_collection_parses_untrusted_hint_once_before_admission(self):
        from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests
        from services.company_discovery.aggregator_bootstrap import extract_bootstrap_hint

        case = AggregatorBootstrapCollectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        with patch('services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint',
                   wraps=extract_bootstrap_hint) as extract:
            case.collect()
        self.assertEqual(extract.call_count, 1,
            'expensive source markup parsing must happen once before transactional admission')

    def _public_request(self, url, _deadline):
        self.network.append(url)
        with self.case.store.engine.connect() as connection:
            existing_native = connection.execute(select(m.company_sources.c.id).where(
                m.company_sources.c.provider == 'greenhouse')).first()
        if self.saw_unconfigured_source_before_first_fetch is None:
            self.saw_unconfigured_source_before_first_fetch = existing_native is None
        request = httpx.Request('GET', url)
        parsed = request.url
        if parsed.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nAllow: /', request=request)
        if parsed.host == 'boards-api.greenhouse.io' and parsed.path == '/v1/boards/acme/jobs/1001':
            return httpx.Response(200, json=self.native_job, request=request)
        if url == self.job_url:
            return httpx.Response(200, text=self.job_html, request=request)
        if url == self.apply_url:
            return httpx.Response(200, text=self.apply_html, request=request)
        return httpx.Response(404, request=request)

    def _greenhouse_board(self, suffix, params=None):
        if not suffix:
            return {'name': self.company}
        return {'jobs': [dict(self.native_job)]}

    def _execute(self, *, mutate=None, operation_mode='FIXTURE', rate_aware_wire=None):
        if mutate:
            mutate()
        output = self.output_dir / 'outputs'
        output.mkdir(exist_ok=True)
        env = {
            'MJOBS_DATABASE_URL': self.case.url,
            'MJOBS_OPERATION_MODE': operation_mode,
            'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0',
            'MJOBS_OUTPUT_DIR': str(output),
        }
        real_resolver = __import__(
            'services.company_discovery.aggregator_workflow',
            fromlist=['corroborate_aggregator_job']).corroborate_aggregator_job

        def controlled_resolver(*args, **kwargs):
            return real_resolver(*args, client_factory=BudgetedPublicClient, **kwargs)

        with patch.dict(os.environ, env), \
             patch.object(PublicCompanyClient, '_request_once', rate_aware_wire or self._public_request), \
             patch.object(Greenhouse, '_get', self._greenhouse_board), \
             patch('services.company_discovery.aggregator_workflow.corroborate_aggregator_job',
                   controlled_resolver):
            return execute_operation(self.request['operation_id'])

    def test_authorized_v2_incompatible_rate_window_blocks_before_egress(self):
        from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests

        case = AggregatorBootstrapCollectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        case._activate_v2()
        raw = case.rows[0]
        self.assertEqual((raw['title'], raw['company_name']), (self.title, self.company))
        raw['description'] += '<p><a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a></p>'
        with case.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == case.source_id).values(rate_limit=1))
        case.collect(**case.parent_operation())
        self.case = case
        with case.store.engine.connect() as connection:
            self.request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            before_source = dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == case.source_id)).mappings().one())
            before_raw = list(connection.execute(select(m.posting_snapshots.c.raw)).scalars())
        clock = [100.0]
        created = []
        original_init = BudgetedPublicClient.__init__

        def create(client, *args, **kwargs):
            created.append(args[0])
            original_init(client, *args, **kwargs)

        def sleep(seconds):
            clock[0] += seconds

        def wire(client, url, deadline):
            client._check()
            client._wait(deadline)
            client._budget()
            return self._public_request(url, deadline)

        from services.company_discovery.aggregator_bootstrap import BootstrapBudget
        with patch('services.company_discovery.aggregator_bootstrap.time.monotonic', lambda: clock[0]), \
             patch('services.company_discovery.aggregator_bootstrap.time.sleep', sleep), \
             patch('services.company_discovery.aggregator_workflow.BootstrapBudget',
                   return_value=BootstrapBudget(timeout_seconds=2.5)), \
             patch.object(BudgetedPublicClient, '__init__', create):
            outcome = self._execute(rate_aware_wire=wire)
        self.assertEqual(outcome['status'], 'BLOCKED', outcome)
        self.assertEqual(outcome['error_category'], 'BOOTSTRAP_RATE_BUDGET_INCOMPATIBLE', outcome)
        self.assertEqual(created, [])
        self.assertEqual(self.network, [])
        with case.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == self.request['operation_id'])).mappings().one()
            request = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request['id'])).mappings().one()
            self.assertEqual(operation['state'], 'BLOCKED')
            self.assertEqual(operation['error'], 'BOOTSTRAP_RATE_BUDGET_INCOMPATIBLE')
            self.assertEqual(operation['attempt_count'], 1)
            self.assertEqual(operation['retry_count'], 0)
            self.assertIsNone(operation['next_attempt_at'])
            self.assertEqual(operation['recovery_metadata'], {'action': 'MANUAL_REVIEW'})
            self.assertEqual(request['authorization'], 'AGGREGATOR_BOOTSTRAP_PENDING')
            self.assertEqual(request['domain_evidence'][0]['version'], 2)
            self.assertEqual(request['domain_evidence'], self.request['domain_evidence'])
            self.assertEqual(dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == case.source_id)).mappings().one()), before_source)
            self.assertEqual(list(connection.execute(select(m.posting_snapshots.c.raw)).scalars()), before_raw)
            self.assertEqual(list(connection.execute(select(m.company_domains)).mappings()), [])
            self.assertEqual(list(connection.execute(select(m.company_sources).where(
                m.company_sources.c.provider == 'greenhouse')).mappings()), [])
            self.assertEqual(list(connection.execute(select(m.audit).where(m.audit.c.action.in_([
                'COMPANY_DISCOVERY_AGGREGATOR_BOOTSTRAP_VERIFIED', 'COMPANY_DOMAIN_ACQUIRED',
                'COMPANY_SOURCE_DISCOVERED']))).mappings()), [])

    def test_worker_revalidates_resolves_persists_domain_then_enrolls_source(self):
        outcome = self._execute()
        self.assertEqual(outcome['status'], 'COMPLETED', outcome)
        self.assertTrue(self.saw_unconfigured_source_before_first_fetch)
        self.assertIn(self.job_url, self.network)
        self.assertIn(self.apply_url, self.network)
        self.assertIn('https://boards-api.greenhouse.io/v1/boards/acme/jobs/1001', self.network)
        self.assertEqual(self.network.count(self.apply_url), 1)
        with self.case.store.engine.connect() as connection:
            request = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request['id'])).mappings().one()
            domains = list(connection.execute(select(m.company_domains)).mappings())
            sources = list(connection.execute(select(m.company_sources)).mappings())
            snapshots = list(connection.execute(select(m.posting_snapshots)).mappings())
            jobs = list(connection.execute(select(m.jobs)).mappings())
            source_audit = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.request['company_id'],
                m.audit.c.action == 'COMPANY_SOURCE_DISCOVERED')).scalar_one()
        self.assertEqual(request['authorization'], 'AGGREGATOR_BOOTSTRAP_VERIFIED')
        self.assertEqual(len(request['domain_evidence']), 3)
        envelope = request['domain_evidence'][1]
        receipt = envelope['receipt']
        self.assertEqual(receipt['execution_scope'], 'CONTROLLED_TRANSPORT')
        self.assertEqual(receipt['native']['tenant'], 'acme')
        self.assertEqual(receipt['native']['job_id'], '1001')
        self.assertEqual(receipt['employer_url'], self.job_url)
        self.assertEqual(receipt['apply_url'], self.apply_url)
        self.assertEqual(len(domains), 1)
        self.assertEqual(domains[0]['company_id'], self.request['company_id'])
        self.assertEqual(domains[0]['domain'], 'acme.example')
        greenhouse_source = next(row for row in sources if row['provider'] == 'greenhouse')
        self.assertTrue(greenhouse_source['enabled'])
        self.assertTrue(greenhouse_source['discovered_automatically'])
        self.assertEqual(greenhouse_source['tenant'], 'acme')
        self.assertEqual(source_audit['company_source_id'], greenhouse_source['id'])
        self.assertEqual(source_audit['discovery_request_id'], self.request['id'])
        self.assertEqual(source_audit['discovery_operation_id'], self.request['operation_id'])
        self.assertEqual(source_audit['aggregator_pending_sha256'], request['domain_evidence'][0]['sha256'])
        self.assertEqual(source_audit['aggregator_envelope_sha256'], envelope['sha256'])
        self.assertEqual(source_audit['aggregator_receipt_sha256'], receipt['sha256'])
        self.assertTrue(any(job['company_id'] == self.request['company_id'] for job in jobs))
        self.assertEqual(snapshots[0]['raw'], self.raw_before)

    def test_personal_worker_runs_same_public_https_chain_without_fixture_marker(self):
        from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests

        case = AggregatorBootstrapCollectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        case._activate_v2()
        with case.store.engine.begin() as connection:
            connection.execute(update(m.workspace_identity).where(
                m.workspace_identity.c.id == 'workspace').values(operation_mode='PERSONAL'))
        raw = case.rows[0]
        raw['description'] += '<p><a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a></p>'
        self.case = case
        self.raw_before = deepcopy(raw)
        self.title, self.company = raw['title'], raw['company_name']
        self.collection = case.collect(**case.parent_operation())
        self.network = []
        self.saw_unconfigured_source_before_first_fetch = None
        self.job_url = 'https://acme.example/careers/listing/senior-python-engineer/1001'
        self.apply_url = 'https://acme.example/careers/apply/senior-python-engineer/1001'
        self.job_html = _job_page(canonical=self.job_url, job_url=self.job_url, job_id='1001',
            title=self.title, company=self.company, apply_href=self.apply_url).replace(
                'https://employer.example', 'https://acme.example')
        self.apply_html = _apply_page(iframe_src=(
            'https://job-boards.greenhouse.io/embed/job_app?for=acme&token=1001'))
        self.native_job = {'id': 1001, 'title': self.title, 'absolute_url': self.job_url,
            'content': '<p>Build Python SQL backend services.</p>'}
        with case.store.engine.connect() as connection:
            self.request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            self.operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.request['operation_id'])).mappings().one())
        self.assertNotIn('fixture_aggregator_bootstrap', self.operation['params'])
        self.assertEqual(self.operation['params']['fetch_mode'], 'PUBLIC_HTTPS')
        outcome = self._execute(operation_mode='PERSONAL')
        self.assertEqual(outcome['status'], 'COMPLETED', outcome)
        self.assertTrue(self.saw_unconfigured_source_before_first_fetch)
        self.assertIn(self.job_url, self.network)
        with case.store.engine.connect() as connection:
            request = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request['id'])).mappings().one()
        self.assertEqual(request['authorization'], 'AGGREGATOR_BOOTSTRAP_VERIFIED')
        self.assertEqual(request['domain_evidence'][1]['receipt']['execution_scope'], 'CONTROLLED_TRANSPORT')

    def test_foreign_greenhouse_job_title_does_not_authorize_aggregator_candidate(self):
        """A cross-job native link is not reciprocal proof for the collected posting."""
        # A real WWR Bloomreach listing linked to a different Czechia vacancy.
        # The API/HTML transport is otherwise valid, so title identity must gate
        # enrollment even when the ATS board and employer site both respond.
        self.native_job['title'] = 'Senior DevOps Engineer - Different Vacancy'

        outcome = self._execute()

        self.assertNotEqual(outcome['status'], 'COMPLETED', outcome)
        self.assertIn('boards-api.greenhouse.io', ' '.join(self.network))
        self.assertEqual(self.case.store.count('company_domains'), 0)
        with self.case.store.engine.connect() as connection:
            request = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request['id'])).mappings().one()
        self.assertNotEqual(request['authorization'], 'AGGREGATOR_BOOTSTRAP_VERIFIED')

    def test_profile_change_after_collection_blocks_all_employer_egress(self):
        changed = deepcopy(self.case.store.profile('fixture'))
        changed['name'] = 'Changed profile revision'
        outcome = self._execute(mutate=lambda: self.case.store.save_profile(changed))
        self.assertNotEqual(outcome['status'], 'COMPLETED', outcome)
        self.assertEqual(self.network, [])
        self.assertEqual(self.case.store.count('company_domains'), 0)
        self.assertEqual(self.case.store.count('company_job_sources'), 1)

    def test_source_policy_change_after_collection_blocks_all_employer_egress(self):
        from services.discovery.sources.collect import configure_source
        outcome = self._execute(mutate=lambda: configure_source(
            self.case.store, 'default', provider='remotive', enabled=False))
        self.assertNotEqual(outcome['status'], 'COMPLETED', outcome)
        self.assertEqual(self.network, [])
        self.assertEqual(self.case.store.count('company_domains'), 0)
        self.assertEqual(self.case.store.count('company_job_sources'), 1)

    def test_cancelled_running_parent_blocks_before_employer_egress(self):
        def cancel_parent():
            with self.case.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == self.parent_lineage['parent_operation_id']).values(
                        cancel_requested=True))
        outcome = self._execute(mutate=cancel_parent)
        self.assertEqual(outcome['status'], 'BLOCKED', outcome)
        self.assertEqual(outcome['error_category'], 'DISCOVERY_BOOTSTRAP_PROOF_CHANGED')
        self.assertEqual(self.network, [])

    def test_expired_running_parent_blocks_before_employer_egress(self):
        def expire_parent():
            with self.case.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == self.parent_lineage['parent_operation_id']).values(
                        lease_expires_at='2000-01-01T00:00:00+00:00'))
        outcome = self._execute(mutate=expire_parent)
        self.assertEqual(outcome['status'], 'BLOCKED', outcome)
        self.assertEqual(outcome['error_category'], 'DISCOVERY_BOOTSTRAP_PROOF_CHANGED')
        self.assertEqual(self.network, [])

    def test_modified_candidate_snapshot_blocks_all_employer_egress(self):
        with self.case.store.engine.begin() as connection:
            connection.execute(update(m.posting_snapshots).where(
                m.posting_snapshots.c.id == self.request['domain_evidence'][0]['binding']['posting_snapshot_id'])
                .values(raw={'id': 999, 'description': 'changed'}))
        outcome = self._execute()
        self.assertNotEqual(outcome['status'], 'COMPLETED', outcome)
        self.assertEqual(self.network, [])
        self.assertEqual(self.case.store.count('company_domains'), 0)

    def test_unmarked_fixture_operation_cannot_fall_through_to_generic_company_worker(self):
        with self.case.store.engine.begin() as connection:
            params = dict(self.operation['params'])
            params.pop('aggregator_bootstrap')
            connection.execute(update(m.operations).where(m.operations.c.id == self.operation['id'])
                .values(params=params))
        outcome = self._execute()
        self.assertNotEqual(outcome['status'], 'COMPLETED', outcome)
        self.assertEqual(self.network, [])


    def _fresh_v2_family(self, *, restore_backup=None, capture_profile_backup=False):
        """Admit a new family normally; never relabel an existing V1 request."""
        from packages.domain.employer_verification import GREENHOUSE_RECIPROCAL
        from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests

        case = AggregatorBootstrapCollectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        profile_id = 'fixture'
        if restore_backup is not None:
            from packages.domain.employer_verification import disabled_preference
            from packages.persistence.profile_restore import apply_profile_restore
            from services.profile_restore import build_restore_plan

            profile_id = 'restored-verification-fixture'
            plan = build_restore_plan(restore_backup, profile_id)
            apply_profile_restore(case.store, plan, self.output_dir / 'restore-files', 'v2-replay-restore')
            restored = case.store.profile(profile_id)
            self.assertEqual(restored['preferences']['company_discovery']['employer_verification'],
                             disabled_preference())
            self.assertFalse(restored['preferences']['company_discovery']['company_discovery_enabled'])
        profile = deepcopy(case.store.profile(profile_id))
        profile['preferences']['company_discovery']['company_discovery_enabled'] = True
        profile['preferences']['company_discovery']['employer_verification'] = {
            'version': 1, 'mode': GREENHOUSE_RECIPROCAL, 'activation_id': None,
        }
        case.store.save_profile(profile, expected_version=profile['version'], require_expected_version=True)
        if capture_profile_backup:
            from services.data_export import export_profile_backup
            # Export the real active profile before this test creates jobs. The
            # later original family still uses this exact saved activation.
            self._v2_profile_backup = export_profile_backup(case.store, profile_id)
        with case.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == case.source_id).values(rate_limit=1))
        raw = case.rows[0]
        self.assertEqual((raw['title'], raw['company_name']), (self.title, self.company))
        raw['description'] += '<p><a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a></p>'
        self.case = case
        self.raw_before = deepcopy(raw)
        if profile_id == 'fixture':
            self.parent_lineage = case.parent_operation()
        else:
            from uuid import uuid4
            from workers.dispatch import enqueue_operation
            from workers.operations import claim

            parent_id = enqueue_operation(case.store, 'DISCOVER', profile_id, {
                'source_id': case.source_id, 'profile_version': case.store.profile(profile_id)['version'],
                'request_id': 'restored-v2-' + uuid4().hex,
            })
            parent = claim(case.store, parent_id)
            self.assertIsNotNone(parent)
            self.parent_lineage = {'parent_operation_id': parent_id,
                                   'parent_operation_attempt': parent['attempt_count']}
        self.collection = case.collect(profile_id=profile_id, **self.parent_lineage)
        self.assertEqual(self.collection['jobs_seen'], 1)
        with case.store.engine.begin() as connection:
            # This test concerns the profile transaction, not a parent lease race.
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.parent_lineage['parent_operation_id']).values(
                    state='COMPLETED', lease_expires_at=None))
            self.request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            self.operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.request['operation_id'])).mappings().one())
        self.pending = deepcopy(self.request['domain_evidence'][0])
        self.assertEqual(self.pending['version'], 2)
        self.assertEqual(self.operation['params']['aggregator_bootstrap'], 'AGGREGATOR_BOOTSTRAP_PENDING_V2')
        self.assertEqual(self.operation['params']['aggregator_bootstrap_version'], 2)
        self.assertEqual(self.pending['binding']['source_policy']['rate_limit'], 1)
        self.assertEqual(self.pending['binding']['verification_policy']['rate_limit'], 30)
        self.network = []
        self.saw_unconfigured_source_before_first_fetch = None
        self._v2_import_call = None

    def _v2_check(self):
        from services.company_discovery.requests import authorize_discovery_request
        return authorize_discovery_request(self.case.store, self.operation)

    def _v2_guard(self, connection):
        from services.company_discovery.requests import authorize_discovery_request
        return authorize_discovery_request(self.case.store, self.operation, connection=connection)

    def _v2_resolver_receipt(self):
        from services.company_discovery.aggregator_bootstrap import corroborate_aggregator_job
        from services.company_discovery.requests import load_aggregator_bootstrap_context

        context = load_aggregator_bootstrap_context(self.case.store, self.operation)
        self.assertEqual(context['pending_intent'], self.pending)
        with patch.object(PublicCompanyClient, '_request_once', self._public_request):
            receipt = corroborate_aggregator_job(
                context['raw'], context['normalized'], context['source_snapshot'],
                context['profile_snapshot'], source_policy=context['source_policy'],
                verification_policy=self.pending['binding']['verification_policy'],
                pending_sha256=self.pending['sha256'], check=self._v2_check,
                client_factory=BudgetedPublicClient,
            )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt['version'], 2)
        self.assertEqual(receipt['execution_scope'], 'CONTROLLED_TRANSPORT')
        return receipt

    def _v2_enroll(self, authorization):
        from services.company_discovery.ats_enrollment import enroll_public_ats
        from services.company_discovery.public_resolution import resolve_public_company

        receipt = authorization['aggregator_bootstrap_receipt']
        candidate = {'company_name': receipt['normalized_projection']['company_name'],
                     'company_website': receipt['employer_url']}
        resolved = resolve_public_company(self.case.store, candidate)
        self.assertEqual(resolved['company_id'], self.request['company_id'])
        self.assertIn('VERIFIED_DOMAIN_MATCH', resolved['evidence'])

        def defer_import(*args, **kwargs):
            # Split the two real production transactions. Nothing is credited as
            # an import until the captured real collect_source call is executed.
            self.assertIs(args[0], self.case.store)
            self.assertEqual(args[2:4], (
                self.pending['binding']['profile_id'], self.pending['binding']['profile_version']))
            self.assertEqual(kwargs['import_guard'], self._v2_guard)
            self._v2_import_call = (args, kwargs)
            return {'jobs_seen': 0}

        with patch('services.discovery.sources.collect.collect_source', defer_import):
            return enroll_public_ats(
                self.case.store, candidate, resolved, receipt['employer_url'], '',
                profile_id=self.pending['binding']['profile_id'],
                profile_version=self.pending['binding']['profile_version'],
                check=self._v2_check, enrollment_guard=self._v2_guard, provider='greenhouse',
                aggregator_bootstrap_authorization=authorization,
            )

    def _v2_stage_call(self, stage):
        """Capture current inputs before the race, with real guards still in place."""
        from services.company_discovery.domain_acquisition import acquire_official_domain_from_aggregator_bootstrap
        from services.company_discovery.requests import persist_aggregator_bootstrap_proof
        from services.discovery.sources.collect import collect_source

        if stage == 'proof':
            return lambda: persist_aggregator_bootstrap_proof(self.case.store, self.operation, self._v2_receipt)
        authorization = self._v2_check()
        if stage == 'domain':
            return lambda: acquire_official_domain_from_aggregator_bootstrap(
                self.case.store, authorization, self._v2_check, self._v2_guard)
        if stage == 'enrollment':
            return lambda: self._v2_enroll(authorization)
        self.assertEqual(stage, 'import')
        self.assertIsNotNone(self._v2_import_call)
        args, kwargs = self._v2_import_call

        def import_native():
            with patch.object(Greenhouse, '_get', self._greenhouse_board):
                return collect_source(*args, **kwargs)
        return import_native

    def _prepare_v2_stage(self, stage):
        self._fresh_v2_family()
        self._v2_receipt = self._v2_resolver_receipt()
        for prerequisite in ('proof', 'domain', 'enrollment', 'import'):
            if prerequisite == stage:
                break
            self._v2_stage_call(prerequisite)()
        else:
            self.fail('Unknown V2 transaction stage')
        return self._v2_stage_call(stage)

    def _v2_protected_rows(self):
        """Include successful import state, excluding only documented attempt effects.

        save_profile may add matches for the new profile version. The original
        pending version's exact matches remain part of the protected write set.
        Collector last_attempt is a separate transaction; health/last_success,
        jobs_seen, source quality and success audits remain compared in full.
        """
        tables = {
            'companies': m.companies, 'domains': m.company_domains,
            'tenants': m.company_ats_tenants, 'company_sources': m.company_sources,
            'jobs': m.jobs, 'routes': m.routes, 'postings': m.postings,
            'snapshots': m.posting_snapshots, 'candidates': m.posting_value_candidates,
        }
        with self.case.store.engine.connect() as connection:
            result = {name: [dict(row) for row in connection.execute(
                select(table).order_by(table.c.id)).mappings()] for name, table in tables.items()}
            result['collectors'] = [
                {key: value for key, value in row.items() if key != 'last_attempt'}
                for row in connection.execute(select(m.job_sources).order_by(m.job_sources.c.id)).mappings()
            ]
            result['matches'] = [dict(row) for row in connection.execute(select(m.matches).where(
                m.matches.c.profile_id == self.pending['binding']['profile_id'],
                m.matches.c.profile_version == self.pending['binding']['profile_version'],
            ).order_by(m.matches.c.id)).mappings()]
            result['quality'] = [dict(row) for row in connection.execute(
                select(m.source_quality_observations).where(m.source_quality_observations.c.succeeded.is_(True))
                .order_by(m.source_quality_observations.c.id)).mappings()]
            result['requests'] = [dict(row) for row in connection.execute(select(
                m.company_discovery_requests.c.id, m.company_discovery_requests.c.authorization,
                m.company_discovery_requests.c.domain_evidence, m.company_discovery_requests.c.operation_id,
                m.company_discovery_requests.c.revision, m.company_discovery_requests.c.profile_id,
                m.company_discovery_requests.c.profile_version, m.company_discovery_requests.c.candidate_id,
            ).order_by(m.company_discovery_requests.c.id)).mappings()]
            result['operation_families'] = [dict(row) for row in connection.execute(select(
                m.operations.c.id, m.operations.c.kind, m.operations.c.entity_id, m.operations.c.params,
                m.operations.c.retry_count, m.operations.c.next_attempt_at,
            ).order_by(m.operations.c.id)).mappings()]
            result['success_audits'] = [dict(row) for row in connection.execute(select(m.audit).where(
                m.audit.c.action.in_([
                    'COMPANY_DISCOVERY_AGGREGATOR_BOOTSTRAP_VERIFIED', 'COMPANY_DOMAIN_ACQUIRED',
                    'COMPANY_SOURCE_DISCOVERED', 'SOURCE_COLLECTION_COMPLETE',
                ])).order_by(m.audit.c.id)).mappings()]
        return result

    def _v2_revoke(self, store=None):
        from packages.domain.employer_verification import disabled_preference

        store = store or self.case.store
        current = deepcopy(store.profile(self.pending['binding']['profile_id']))
        current['preferences']['company_discovery']['employer_verification'] = disabled_preference()
        return store.save_profile(current, expected_version=current['version'], require_expected_version=True)

    def _assert_v2_rejected(self, call):
        from services.company_discovery.public_client import PublicFetchError

        with self.assertRaises(PublicFetchError) as caught:
            call()
        self.assertIn(caught.exception.code, {
            'DISCOVERY_BOOTSTRAP_PROOF_CHANGED', 'DISCOVERY_AUTHORIZATION_CHANGED',
            'DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED', 'DISCOVERY_PROFILE_CHANGED',
        })

    def _assert_v2_stage_committed(self, stage, before, after):
        action = {
            'proof': 'COMPANY_DISCOVERY_AGGREGATOR_BOOTSTRAP_VERIFIED',
            'domain': 'COMPANY_DOMAIN_ACQUIRED',
            'enrollment': 'COMPANY_SOURCE_DISCOVERED',
            'import': 'SOURCE_COLLECTION_COMPLETE',
        }[stage]
        old_audits = {row['id'] for row in before['success_audits']}
        added = [row for row in after['success_audits'] if row['id'] not in old_audits]
        self.assertEqual([row['action'] for row in added], [action])
        if stage == 'proof':
            row = next(row for row in after['requests'] if row['id'] == self.request['id'])
            self.assertEqual(row['authorization'], 'AGGREGATOR_BOOTSTRAP_VERIFIED')
            self.assertEqual(row['domain_evidence'][1]['receipt'], self._v2_receipt)
        elif stage == 'domain':
            self.assertEqual(len(after['domains']), len(before['domains']) + 1)
            self.assertEqual(after['domains'][0]['domain'], 'acme.example')
            self.assertTrue(after['domains'][0]['verified'])
        elif stage == 'enrollment':
            self.assertEqual(len(after['tenants']), len(before['tenants']) + 1)
            self.assertEqual(len(after['company_sources']), len(before['company_sources']) + 1)
            self.assertEqual(len(after['collectors']), len(before['collectors']) + 1)
            self.assertIsNotNone(self._v2_import_call)
        else:
            self.assertGreater(len(after['snapshots']), len(before['snapshots']))
            self.assertGreater(len(after['candidates']), len(before['candidates']))
            self.assertGreater(len(after['quality']), len(before['quality']))
            collector = next(row for row in after['collectors'] if row['provider'] == 'greenhouse')
            self.assertEqual(collector['jobs_seen'], 1)
            self.assertIsNotNone(collector['last_success'])
            self.assertEqual(collector['health_status'], 'HEALTHY')

    def _postgres_v2_race(self, stage, ordering, *, rollback=False):
        if self.case.store.engine.dialect.name != 'postgresql':
            self.skipTest('This ordering requires the actual disposable PostgreSQL subclass')
        call = self._prepare_v2_stage(stage)
        self.assertEqual(self.case.store.engine.dialect.name, 'postgresql')
        with self.case.store.engine.connect() as connection:
            self.assertEqual(connection.execute(text(
                "SELECT setting::int FROM pg_settings WHERE name = 'lock_timeout'"
            )).scalar_one(), 5000, 'The inherited disposable-PG fixture must retain its 5s lock bound')
        writer = Store(self.case.url)
        self.addCleanup(writer.engine.dispose)
        before = self._v2_protected_rows()
        with _ProfileTransactionRace(self.case, writer, ordering, rollback=rollback) as race:
            if ordering == 'revoker_committed_first':
                race.release_writer.set()
                self._v2_revoke(writer)
                self.assertTrue(race.writer_locked.is_set())
                before = self._v2_protected_rows()
                self._assert_v2_rejected(lambda: race.call_stage(call))
                self.assertIsInstance(race.stage_pid, int)
                self.assertGreater(race.stage_pid, 0)
                self.assertNotEqual(race.stage_pid, race.writer_pid)
                self.assertEqual(self._v2_protected_rows(), before)
                return

            def start_writer():
                race.start(lambda: self._v2_revoke(writer), race.writer_result, race.writer_done,
                           name='mjobs-v2-profile-writer')

            def start_stage():
                race.start(lambda: race.call_stage(call), race.stage_result, race.stage_done,
                           name='mjobs-v2-' + stage)

            if ordering == 'writer_first':
                start_writer()
                self.assertTrue(race.writer_locked.wait(5), race.writer_result)
                self.assertFalse(race.writer_done.is_set())
                start_stage()
                # A waited lock would hit the fixture's 5s lock_timeout. Completion
                # while the real writer is still held proves the SKIP LOCKED path.
                self.assertTrue(race.stage_done.wait(3), 'V2 guard waited for the held profile writer')
                self.assertTrue(race.share_seen.is_set(), race.stage_result)
                self.assertEqual(race.share_rows, 0)
                self.assertNotEqual(race.stage_pid, race.writer_pid)
                self.assertFalse(race.writer_done.is_set())
                error = race.stage_result.get('error')
                from services.company_discovery.public_client import PublicFetchError
                self.assertIsInstance(error, PublicFetchError)
                self.assertEqual(error.code, 'DISCOVERY_BOOTSTRAP_PROOF_CHANGED')
                self.assertEqual(self._v2_protected_rows(), before)
                race.release_writer.set()
            else:
                self.assertEqual(ordering, 'share_first')
                start_stage()
                self.assertTrue(race.share_seen.wait(5), race.stage_result)
                self.assertEqual(race.share_rows, 1)
                start_writer()
                self.assertTrue(race.writer_update_started.wait(5), race.writer_result)
                self.assertNotEqual(race.stage_pid, race.writer_pid)
                observation = race.database_blocking_observation()
                self.assertNotIn(observation['observer_pid'], {race.stage_pid, race.writer_pid})
                self.assertIn(race.stage_pid, observation['blockers'])
                self.assertEqual(observation['wait_type'], 'Lock')
                self.assertFalse(race.writer_locked.is_set())
                self.assertFalse(race.writer_done.is_set())
                race.release_share.set()
                self.assertTrue(race.stage_done.wait(5), race.stage_result)
                self.assertTrue(race.writer_locked.wait(5), race.writer_result)
                self.assertFalse(race.writer_done.is_set())
                # The incompatible normal UPDATE now owns the row. It could not
                # acquire that lock before the actual stage commit/rollback.
                after_stage = self._v2_protected_rows()
                if rollback:
                    self.assertIsInstance(race.stage_result.get('error'), _ControlledV2Rollback)
                    self.assertEqual(after_stage, before)
                    next_call = call
                else:
                    self.assertNotIn('error', race.stage_result, race.stage_result)
                    self._assert_v2_stage_committed(stage, before, after_stage)
                    next_stage = {'proof': 'domain', 'domain': 'enrollment',
                                  'enrollment': 'import', 'import': 'import'}[stage]
                    # Capture next-stage inputs while the revoker is still held
                    # at its CAS UPDATE; no stale authority check is substituted.
                    next_call = self._v2_stage_call(next_stage)
                race.release_writer.set()

            self.assertTrue(race.writer_done.wait(5), race.writer_result)
            self.assertNotIn('error', race.writer_result, race.writer_result)
            current = self.case.store.profile(self.pending['binding']['profile_id'])
            self.assertEqual(current['preferences']['company_discovery']['employer_verification']['mode'], 'DISABLED')
            self.assertIsNone(current['preferences']['company_discovery']['employer_verification']['activation_id'])
            after_revoke = self._v2_protected_rows()
            self._assert_v2_rejected(next_call if ordering == 'share_first' else call)
            self.assertEqual(self._v2_protected_rows(), after_revoke)

    def test_v2_postgres_proof_revoker_commits_first(self):
        self._postgres_v2_race('proof', 'revoker_committed_first')

    def test_v2_postgres_domain_revoker_commits_first(self):
        self._postgres_v2_race('domain', 'revoker_committed_first')

    def test_v2_postgres_enrollment_revoker_commits_first(self):
        self._postgres_v2_race('enrollment', 'revoker_committed_first')

    def test_v2_postgres_import_revoker_commits_first(self):
        self._postgres_v2_race('import', 'revoker_committed_first')

    def test_v2_postgres_proof_share_precedes_normal_profile_update(self):
        self._postgres_v2_race('proof', 'share_first')

    def test_v2_postgres_domain_share_precedes_normal_profile_update(self):
        self._postgres_v2_race('domain', 'share_first')

    def test_v2_postgres_enrollment_share_precedes_normal_profile_update(self):
        self._postgres_v2_race('enrollment', 'share_first')

    def test_v2_postgres_import_share_precedes_normal_profile_update(self):
        self._postgres_v2_race('import', 'share_first')

    def test_v2_postgres_proof_skips_locked_profile_writer(self):
        self._postgres_v2_race('proof', 'writer_first')

    def test_v2_postgres_domain_skips_locked_profile_writer(self):
        self._postgres_v2_race('domain', 'writer_first')

    def test_v2_postgres_enrollment_skips_locked_profile_writer(self):
        self._postgres_v2_race('enrollment', 'writer_first')

    def test_v2_postgres_import_skips_locked_profile_writer(self):
        self._postgres_v2_race('import', 'writer_first')

    def test_v2_postgres_rollback_releases_each_actual_stage_profile_lock(self):
        for stage in ('proof', 'domain', 'enrollment', 'import'):
            with self.subTest(stage=stage):
                self._postgres_v2_race(stage, 'share_first', rollback=True)

    def test_v2_committed_disable_reenable_rejects_old_grant_at_each_stage(self):
        from packages.domain.employer_verification import GREENHOUSE_RECIPROCAL

        for stage in ('proof', 'domain', 'enrollment', 'import'):
            with self.subTest(stage=stage):
                call = self._prepare_v2_stage(stage)
                old_id = self.pending['binding']['verification_policy']['activation_id']
                self._v2_revoke()
                profile = deepcopy(self.case.store.profile(self.pending['binding']['profile_id']))
                profile['preferences']['company_discovery']['employer_verification'] = {
                    'version': 1, 'mode': GREENHOUSE_RECIPROCAL, 'activation_id': None,
                }
                self.case.store.save_profile(profile, expected_version=profile['version'], require_expected_version=True)
                current = self.case.store.profile(profile['id'])
                self.assertNotEqual(current['preferences']['company_discovery']['employer_verification']['activation_id'], old_id)
                before = self._v2_protected_rows()
                self._assert_v2_rejected(call)
                self.assertEqual(self._v2_protected_rows(), before)

    def test_v2_actual_restore_new_family_rejects_original_family_receipt(self):
        from services.company_discovery.requests import (
            authorize_discovery_request, persist_aggregator_bootstrap_proof,
        )
        self._fresh_v2_family(capture_profile_backup=True)
        self._v2_receipt = self._v2_resolver_receipt()
        original_store = self.case.store
        original_operation = deepcopy(self.operation)
        original_pending = deepcopy(self.pending)
        original_receipt = deepcopy(self._v2_receipt)
        original_profile = deepcopy(original_store.profile(original_pending['binding']['profile_id']))
        backup = deepcopy(self._v2_profile_backup)
        self._fresh_v2_family(restore_backup=backup)
        self.assertNotEqual(self.pending['binding']['profile_id'], original_pending['binding']['profile_id'])
        self.assertNotEqual(self.pending['binding']['verification_policy']['activation_id'],
                            original_pending['binding']['verification_policy']['activation_id'])
        self.assertNotEqual(self.pending['sha256'], original_pending['sha256'])
        before = self._v2_protected_rows()
        self._assert_v2_rejected(lambda: persist_aggregator_bootstrap_proof(
            self.case.store, self.operation, original_receipt))
        self.assertEqual(self._v2_protected_rows(), before)

        # Positive control: the restored target's real new family can persist its
        # own controlled receipt. No request/operation authority row is fabricated.
        self._v2_receipt = self._v2_resolver_receipt()
        self._v2_stage_call('proof')()
        self._assert_v2_stage_committed('proof', before, self._v2_protected_rows())
        self.assertEqual(original_store.profile(original_profile['id']), original_profile)
        original_authority = authorize_discovery_request(original_store, original_operation)
        self.assertEqual(original_authority['authorization'], 'AGGREGATOR_BOOTSTRAP_PENDING')
        self.assertEqual(original_authority['aggregator_bootstrap_pending'], original_pending)

    def _assert_v2_worker_terminal_without_retry(self, outcome, before, *, error):
        self.assertEqual(outcome['status'], 'BLOCKED', outcome)
        self.assertEqual(self._v2_protected_rows(), before)
        with self.case.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.request['operation_id'])).mappings().one())
            retry_audits = list(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == operation['id'], m.audit.c.action == 'OPERATION_RETRY_SCHEDULED')).mappings())
        self.assertEqual(operation['state'], 'BLOCKED')
        self.assertEqual(operation['error'], error)
        self.assertEqual(operation['attempt_count'], 1)
        self.assertEqual(operation['retry_count'], 0)
        self.assertIsNone(operation['next_attempt_at'])
        self.assertIsNotNone(operation['completed_at'])
        self.assertIsNone(operation['lease_expires_at'])
        self.assertNotEqual(operation['current_step'], 'RETRY_WAIT')
        self.assertEqual(operation['recovery_metadata']['action'], 'MANUAL_REVIEW')
        self.assertEqual(retry_audits, [])

    def test_v2_actual_worker_completes_without_changing_listing_policy(self):
        self._fresh_v2_family()
        profile = deepcopy(self.case.store.profile(self.pending['binding']['profile_id']))
        before = self._v2_protected_rows()
        outcome = self._execute()
        self.assertEqual(outcome['status'], 'COMPLETED', outcome)
        self.assertEqual(self.case.store.profile(profile['id']), profile)
        after = self._v2_protected_rows()
        request = next(row for row in after['requests'] if row['id'] == self.request['id'])
        self.assertEqual(request['authorization'], 'AGGREGATOR_BOOTSTRAP_VERIFIED')
        self.assertEqual(request['domain_evidence'][0], self.pending)
        self.assertEqual(request['domain_evidence'][1]['version'], 2)
        receipt = request['domain_evidence'][1]['receipt']
        self.assertEqual(receipt['version'], 2)
        self.assertEqual(receipt['execution_scope'], 'CONTROLLED_TRANSPORT')
        self.assertEqual(receipt['pending_sha256'], self.pending['sha256'])
        self.assertEqual(len(request['domain_evidence']), 3)
        collector = next(row for row in after['collectors'] if row['id'] == self.case.source_id)
        before_collector = next(row for row in before['collectors'] if row['id'] == self.case.source_id)
        self.assertEqual(collector, before_collector)
        self.assertEqual(collector['rate_limit'], 1)
        added = [row['action'] for row in after['success_audits']
                 if row['id'] not in {old['id'] for old in before['success_audits']}]
        self.assertCountEqual(added, [
            'COMPANY_DISCOVERY_AGGREGATOR_BOOTSTRAP_VERIFIED', 'COMPANY_DOMAIN_ACQUIRED',
            'COMPANY_SOURCE_DISCOVERED', 'SOURCE_COLLECTION_COMPLETE',
        ])
        with self.case.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == self.request['operation_id'])).mappings().one()
        self.assertEqual(operation['state'], 'COMPLETED')
        self.assertEqual(operation['attempt_count'], 1)
        self.assertEqual(operation['retry_count'], 0)
        self.assertIsNone(operation['next_attempt_at'])

    def test_v2_actual_worker_authority_revocation_has_no_retry_or_fallback(self):
        from workers.retries import discovery_retry_plan
        from services.company_discovery.flywheel import discover_company_source

        self._fresh_v2_family()
        self._v2_revoke()
        before = self._v2_protected_rows()
        with patch('workers.tasks.discovery_retry_plan', wraps=discovery_retry_plan) as retry, \
             patch('workers.tasks.discover_company_source', wraps=discover_company_source) as fallback:
            outcome = self._execute()
        retry.assert_not_called()
        fallback.assert_not_called()
        self.assertEqual(self.network, [])
        self._assert_v2_worker_terminal_without_retry(
            outcome, before, error='DISCOVERY_BOOTSTRAP_PROOF_CHANGED')

    def test_v2_actual_worker_429_and_503_retry_after_are_terminal(self):
        from workers.retries import discovery_retry_plan
        from services.company_discovery.flywheel import discover_company_source

        for status in (429, 503):
            with self.subTest(status=status):
                self._fresh_v2_family()
                before = self._v2_protected_rows()
                rejected = []

                def wire(client, url, deadline):
                    request = httpx.Request('GET', url)
                    if request.url.path == '/robots.txt':
                        return self._public_request(url, deadline)
                    self.network.append(url)
                    rejected.append(url)
                    return httpx.Response(status, headers={'Retry-After': '120'},
                                          text='controlled unavailable', request=request)

                # Keep actual BudgetedPublicClient, robots, _get status handling,
                # workflow and execute_operation. Only the socket is controlled.
                with patch('workers.tasks.discovery_retry_plan', wraps=discovery_retry_plan) as retry, \
                     patch('workers.tasks.discover_company_source', wraps=discover_company_source) as fallback:
                    outcome = self._execute(rate_aware_wire=wire)
                retry.assert_not_called()
                fallback.assert_not_called()
                self.assertEqual(rejected, ['https://boards-api.greenhouse.io/v1/boards/acme/jobs/1001'])
                self.assertEqual(self.network.count(rejected[0]), 1)
                self.assertNotIn(self.job_url, self.network)
                self.assertNotIn(self.apply_url, self.network)
                self._assert_v2_worker_terminal_without_retry(outcome, before,
                    error='HTTP_STATUS_BLOCKED' if status == 429 else 'HTTP_STATUS_ERROR')


if __name__ == '__main__':
    unittest.main()
