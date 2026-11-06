"""Staged aggregator→native bootstrap through the actual company worker."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.aggregator_bootstrap import BudgetedPublicClient, canonical_digest
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.sources.greenhouse import Greenhouse
from tests.integration.test_official_domain_canonical_apply_proof import _apply_page, _job_page
from workers.tasks import execute_operation


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

    def test_personal_no_hint_job_cannot_replace_queued_typed_request(self):
        from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests

        case = AggregatorBootstrapCollectionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        with case.store.engine.begin() as connection:
            connection.execute(update(m.workspace_identity).where(
                m.workspace_identity.c.id == 'workspace').values(operation_mode='PERSONAL'))
        first = case.rows[0]
        first['description'] += '<p><a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a></p>'
        case.collect()
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
        case.collect()
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
                'profile_version': self.case.store.profile('fixture')['version']},
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

    def _execute(self, *, mutate=None, operation_mode='FIXTURE'):
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
             patch.object(PublicCompanyClient, '_request_once', self._public_request), \
             patch.object(Greenhouse, '_get', self._greenhouse_board), \
             patch('services.company_discovery.aggregator_workflow.corroborate_aggregator_job',
                   controlled_resolver):
            return execute_operation(self.request['operation_id'])

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
        with case.store.engine.begin() as connection:
            connection.execute(update(m.workspace_identity).where(
                m.workspace_identity.c.id == 'workspace').values(operation_mode='PERSONAL'))
        raw = case.rows[0]
        raw['description'] += '<p><a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a></p>'
        self.case = case
        self.raw_before = deepcopy(raw)
        self.title, self.company = raw['title'], raw['company_name']
        self.collection = case.collect()
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


if __name__ == '__main__':
    unittest.main()
