"""Worker-to-JSON correlation proofs against a disposable durable database."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.discovery.sources.collect import collect_source, configure_source
from services.documents.generate import prepare
from services.documents.errors import PreparationError
from workers.operation_log_context import resolve_operation_log_context
from workers.operations import OperationCancelled
from workers.structured_logging import log_operation_finished
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation
from workers.tasks import execute_operation


class OperationLogContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'context.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)
        self.server = fixture_server()
        self.origin = self.server.__enter__()
        discover(self.store, self.origin, "fixture")
        # A route with exactly one posting has an unambiguous durable correlation.
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.postings)).mappings().all()
            self.posting = next(dict(row) for row in rows if sum(
                other['job_id'] == row['job_id'] for other in rows) == 1)
        self.job_id = self.posting['job_id']

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.store.engine.dispose()
        self.directory.cleanup()

    def execute(self, operation_id, origin=None):
        origin = origin or self.origin
        def persisted_then_log(operation, result, error, duration):
            with self.store.engine.connect() as connection:
                saved = connection.execute(select(m.operations.c.state).where(
                    m.operations.c.id == operation_id)).scalar_one()
            self.assertEqual(saved, result)
            log_operation_finished(operation, result, error, duration)
        with patch.dict(os.environ, MJOBS_DATABASE_URL=self.store.url,
                        MJOBS_OPERATION_MODE="FIXTURE", MJOBS_OUTPUT_DIR=str(self.root),
                        MJOBS_MOCK_ORIGIN=origin), patch(
                            "workers.tasks.log_operation_finished", side_effect=persisted_then_log), self.assertLogs(
                            "mjobs.operations", level="INFO") as captured:
            result = execute_operation(operation_id)
        self.assertEqual(len(captured.records), 1)
        record = json.loads(captured.records[0].getMessage())
        with self.store.engine.connect() as connection:
            state = connection.execute(select(m.operations.c.state).where(
                m.operations.c.id == operation_id)).scalar_one()
        self.assertEqual(record['result'], state)
        self.assertEqual(result['status'], state)
        return result, record

    def test_prepare_worker_emits_real_persisted_posting_and_relationships(self):
        operation_id = enqueue_operation(self.store, 'PREPARE', 'fixture', {'job_id': self.job_id})
        result, record = self.execute(operation_id)
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(record.get('posting_id'), self.posting['id'])
        reopened = Store(self.store.url)
        try:
            with reopened.engine.connect() as connection:
                application = connection.execute(select(m.applications).where(
                    m.applications.c.profile_id == 'fixture',
                    m.applications.c.job_id == self.job_id)).mappings().one()
                company_id = connection.execute(select(m.jobs.c.company_id).where(
                    m.jobs.c.id == self.job_id)).scalar_one()
            self.assertEqual(record['application_id'], application['id'])
            self.assertEqual(record['job_id'], self.job_id)
            self.assertEqual(record['company_id'], company_id)
            self.assertEqual(record['ats'], 'mock-ats')
        finally:
            reopened.engine.dispose()

    def resolve(self, operation_id, **overrides):
        with self.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())
        return resolve_operation_log_context(self.store, {**operation, **overrides})

    def assert_private_absent(self, record):
        message = json.dumps(record)
        for value in ('CANARY', 'private.test', 'https://', 'raw exception', 'params',
                      'profile_id', 'tenant', 'candidate', 'resume_text'):
            self.assertNotIn(value, message)
        self.assertLess(len(message), 2000)

    def test_source_collection_worker_logs_durable_source_company_not_batch_job(self):
        import httpx
        from tests.integration.test_greenhouse import RAW
        source_id = configure_source(self.store, 'operation-context', jitter_seconds=0)
        requests = []

        def transport(request):
            requests.append(request)
            self.assertEqual(request.method, 'GET')
            if request.url.path.endswith('/jobs'):
                return httpx.Response(200, json={'jobs': [RAW], 'meta': {'total': 1}})
            return httpx.Response(200, json={'name': 'CANARY employer'})

        def collect(store, source_id, profile_id, **kwargs):
            return collect_source(store, source_id, profile_id,
                                  transport=httpx.MockTransport(transport), **kwargs)

        operation_id = enqueue_operation(self.store, 'DISCOVER', 'fixture', {
            'source_id': source_id, 'api_key': 'CANARY', 'job_id': self.job_id,
            'company_id': 'FORGED_COMPANY', 'posting_id': self.posting['id'],
        })
        with patch('workers.tasks.collect_source', side_effect=collect) as provider:
            result, record = self.execute(operation_id)
        provider.assert_called_once()
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertTrue(requests)
        with self.store.engine.connect() as connection:
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one()
            company = connection.execute(select(m.company_sources.c.company_id).where(
                m.company_sources.c.collector_source_id == source_id)).scalar_one()
            self.assertTrue(connection.execute(select(m.matches.c.id).where(
                m.matches.c.profile_id == 'fixture')).first())
        self.assertEqual(record['source_id'], source['id'])
        self.assertEqual(record['source'], source['provider'])
        self.assertEqual(record['company_id'], company)
        self.assertEqual(record['ats'], 'greenhouse')
        for field in ('posting_id', 'job_id', 'application_id'):
            self.assertNotIn(field, record)
        self.assert_private_absent(record)
        # Follow the collected job through actual PREPARE; source lineage survives
        # canonical attributes and is derived from persisted posting observations.
        with self.store.engine.connect() as connection:
            posting = connection.execute(select(m.postings).join(
                m.jobs, m.jobs.c.id == m.postings.c.job_id).where(
                    m.jobs.c.company_id == company)).mappings().one()
        preparation = enqueue_operation(self.store, 'PREPARE', 'fixture', {'job_id': posting['job_id']})
        prepared, prepared_log = self.execute(preparation)
        self.assertEqual(prepared['status'], 'COMPLETED', prepared)
        self.assertEqual(prepared_log['posting_id'], posting['id'])
        self.assertEqual(prepared_log['source_id'], source_id)
        self.assertEqual(prepared_log['source'], 'greenhouse')
        self.assertEqual(prepared_log['ats'], 'greenhouse')
        self.assert_private_absent(prepared_log)

    def test_application_worker_logs_persisted_posting_after_real_browser_outcome(self):
        application_id = prepare(self.store, self.job_id, 'fixture', self.root)
        operation_id = enqueue_operation(self.store, 'APPLY_MOCK', application_id, {
            'submit': False, 'api_key': 'CANARY', 'company_id': 'FORGED_COMPANY',
            'posting_id': 'FORGED_POSTING', 'source': 'https://private.test/?token=CANARY',
        })
        result, record = self.execute(operation_id)
        self.assertEqual(result['status'], 'COMPLETED', result)
        reopened = Store(self.store.url)
        try:
            saved = reopened.application(application_id)
            self.assertEqual(saved['state'], 'DRY_RUN_COMPLETE')
            self.assertEqual(record['application_id'], saved['id'])
            self.assertEqual(record['job_id'], saved['job_id'])
            self.assertEqual(record['posting_id'], self.posting['id'])
        finally:
            reopened.engine.dispose()
        self.assertEqual(record['ats'], 'mock-ats')
        self.assertNotIn('FORGED', json.dumps(record))
        self.assert_private_absent(record)

    def test_failed_and_cancelled_prepare_keep_context_without_raw_exception(self):
        for number, (failure, expected) in enumerate((
            (PreparationError('DOCUMENT_GENERATION_FAILED', 'raw exception CANARY'), 'DEAD_LETTERED'),
            (OperationCancelled('raw exception CANARY'), 'CANCELLED'),
        )):
            with self.subTest(result=expected):
                operation_id = enqueue_operation(self.store, 'PREPARE', 'fixture', {
                    'job_id': self.job_id, 'request_id': f'failed-{number}', 'resume_text': 'CANARY'})
                with patch('workers.tasks.prepare', side_effect=failure) as provider:
                    result, record = self.execute(operation_id)
                provider.assert_called_once()
                self.assertEqual(result['status'], expected)
                self.assertEqual(record['posting_id'], self.posting['id'])
                self.assertEqual(record['job_id'], self.job_id)
                self.assert_private_absent(record)

    def test_enrichment_database_failure_preserves_completed_and_failed_outcomes(self):
        from workers import tasks
        from sqlalchemy.exc import OperationalError

        def unavailable(store, operation):
            with patch.object(store.engine, 'connect', side_effect=OperationalError(
                    'CANARY query', {}, Exception('raw exception CANARY'))):
                return resolve_operation_log_context(store, operation)

        operation_ids = []
        for number, failure in enumerate((None, PreparationError('DOCUMENT_GENERATION_FAILED',
                                                                 'raw exception CANARY'))):
            operation_id = enqueue_operation(self.store, 'PREPARE', 'fixture', {
                'job_id': self.job_id, 'request_id': f'db-failed-{number}'})
            operation_ids.append(operation_id)
            with patch.object(tasks, '_resolve_application_log_context', side_effect=unavailable), patch(
                    'workers.tasks.prepare', wraps=prepare, side_effect=failure) as provider:
                result, record = self.execute(operation_id)
            provider.assert_called_once()
            self.assertEqual(result['status'], 'COMPLETED' if failure is None else 'DEAD_LETTERED')
            for field in ('job_id', 'posting_id', 'company_id', 'application_id', 'source', 'ats'):
                self.assertNotIn(field, record)
            self.assert_private_absent(record)
        # Neither the completed nor failed delivery may replay after enrichment failed.
        with patch.dict(os.environ, MJOBS_DATABASE_URL=self.store.url, MJOBS_OPERATION_MODE='FIXTURE'), \
                patch('workers.tasks.prepare', side_effect=AssertionError('must not replay')), \
                self.assertNoLogs('mjobs.operations', level='INFO'):
            for operation_id in operation_ids:
                self.assertEqual(execute_operation(operation_id)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')

    def test_unpersisted_invalid_and_cross_profile_prepare_references_fail_closed(self):
        self.store.save_profile({**PROFILE, 'id': 'other', 'email': 'other@example.test'})
        for number, (profile_id, job_id) in enumerate((
            ('fixture', 'missing-job'), ('missing-profile', self.job_id),
            ('other', self.job_id), ('fixture', []),
            ('fixture', 'https://private.test/?token=CANARY'),
        )):
            with self.subTest(profile=profile_id, job=job_id):
                operation_id = enqueue_operation(self.store, 'PREPARE', profile_id, {
                    'job_id': job_id, 'request_id': f'invalid-{number}'})
                self.assertEqual(self.resolve(operation_id), {})
        self.assertEqual(resolve_operation_log_context(self.store, {
            'id': 'missing-operation', 'kind': 'PREPARE', 'entity_id': 'fixture',
            'params': {'job_id': self.job_id}}), {})

    def test_invalid_source_worker_omits_nonexistent_source_and_forged_company(self):
        operation_id = enqueue_operation(self.store, 'DISCOVER', 'fixture', {
            'source_id': 'unpersisted-source', 'company_id': 'FORGED_COMPANY'})
        result, record = self.execute(operation_id)
        self.assertEqual(result['status'], 'DEAD_LETTERED')
        for field in ('source_id', 'source', 'company_id', 'ats'):
            self.assertNotIn(field, record)
        self.assert_private_absent(record)

    def test_application_profile_and_route_mismatch_fail_closed(self):
        application_id = prepare(self.store, self.job_id, 'fixture', self.root)
        for number, hints in enumerate(({'profile_id': 'other'}, {'application_id': 'other'},
                                        {'route_id': 'unrelated-route'})):
            operation_id = enqueue_operation(self.store, 'APPLICATION_FORM_REVIEW', application_id,
                                             {**hints, 'request_id': f'mismatch-{number}'})
            self.assertEqual(self.resolve(operation_id), {})
        # Even if params claim the right profile, the stored route must belong to the job/company.
        other_route = None
        with self.store.engine.begin() as connection:
            other_route = connection.execute(select(m.routes.c.id).where(
                m.routes.c.job_id != self.job_id)).scalars().first()
            connection.execute(update(m.applications).where(m.applications.c.id == application_id)
                               .values(route_id=other_route))
        operation_id = enqueue_operation(self.store, 'APPLY_MOCK', application_id, {'submit': False})
        self.assertEqual(self.resolve(operation_id), {})

    def test_ambiguous_posting_never_selects_an_arbitrary_row(self):
        with self.store.engine.begin() as connection:
            duplicate = {**self.posting, 'id': 'another-posting', 'source_job_id': 'another-posting'}
            connection.execute(m.postings.insert().values(**duplicate))
        operation_id = enqueue_operation(self.store, 'PREPARE', 'fixture', {'job_id': self.job_id})
        result, record = self.execute(operation_id)
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(record['job_id'], self.job_id)
        self.assertNotIn('posting_id', record)
        self.assertNotIn('source', record)
        self.assert_private_absent(record)

    def test_resolver_reloads_durable_hints_and_rejects_identity_mismatch(self):
        operation_id = enqueue_operation(self.store, 'PREPARE', 'fixture', {'job_id': self.job_id})
        context = self.resolve(operation_id, params={'job_id': 'FORGED_JOB'}, company_id='FORGED_COMPANY')
        self.assertEqual(context['job_id'], self.job_id)
        self.assertNotIn('FORGED', json.dumps(context))
        self.assertEqual(self.resolve(operation_id, entity_id='other'), {})
        self.assertEqual(self.resolve(operation_id, kind='APPLY_MOCK'), {})

    def test_missing_application_does_not_log_entity_or_params(self):
        operation_id = enqueue_operation(self.store, 'APPLICATION_FORM_REVIEW', 'missing-application', {
            'profile_id': 'fixture', 'application_id': 'missing-application',
            'job_id': self.job_id, 'posting_id': self.posting['id'],
            'route_url': 'https://private.test/?token=CANARY'})
        result, record = self.execute(operation_id)
        self.assertEqual(result['status'], 'DEAD_LETTERED')
        for field in ('application_id', 'job_id', 'posting_id', 'source_id', 'ats'):
            self.assertNotIn(field, record)
        self.assert_private_absent(record)

    def test_source_and_company_refresh_context_are_from_persisted_association(self):
        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.company_sources)).mappings().first())
        for number, (entity_id, params, expected) in enumerate((
            (source['id'], {'source_id': source['id'], 'company_id': 'FORGED_COMPANY'}, source['company_id']),
            ('missing-source', {'source_id': source['id']}, None),
            (source['id'], {'source_id': 'unrelated-source'}, None),
        )):
            operation_id = enqueue_operation(self.store, 'COMPANY_SOURCE_REFRESH', entity_id,
                                             {**params, 'request_id': f'refresh-{number}'})
            context = self.resolve(operation_id)
            if expected:
                self.assertEqual(context['source_id'], source['id'])
                self.assertEqual(context['company_id'], expected)
                self.assertEqual(context['source'], 'mock-ats')
            else:
                self.assertEqual(context, {})

    def test_company_discovery_uses_durable_operation_link_not_candidate(self):
        with self.store.engine.begin() as connection:
            source = dict(connection.execute(select(m.company_sources)).mappings().one())
            company_id = connection.execute(select(m.jobs.c.company_id).where(
                m.jobs.c.id == self.job_id)).scalar_one()
            connection.execute(update(m.company_sources).where(
                m.company_sources.c.id == source['id']).values(company_id=company_id))
            source['company_id'] = company_id
            connection.execute(m.posting_snapshots.insert().values(
                id='context-snapshot', posting_id=self.posting['id'], raw={}, sha256='a' * 64,
                retrieved_at='2026-10-15T00:00:00Z'))
            connection.execute(m.source_quality_observations.insert().values(
                id='context-observation', company_source_id=source['id'],
                observed_at='2026-10-15T00:00:00Z', succeeded=True,
                source_endpoint=self.posting['source_url']))
            candidate = {'id': 'context-candidate', 'company_source_id': source['id']}
            connection.execute(m.posting_value_candidates.insert().values(
                **candidate, posting_id=self.posting['id'], posting_snapshot_id='context-snapshot',
                quality_observation_id='context-observation', observed_at='2026-10-15T00:00:00Z',
                normalized={}, application_url=self.posting['application_url']))
        operation_id = enqueue_operation(self.store, 'COMPANY_DISCOVER', source['company_id'], {
            'profile_id': 'fixture', 'candidate': {'company_name': 'CANARY',
            'company_website': 'https://private.test/?token=CANARY'}, 'discovery_request_id': 'request-1'})
        with self.store.engine.begin() as connection:
            connection.execute(m.company_discovery_requests.insert().values(
                id='request-1', company_id=source['company_id'], company_source_id=source['id'],
                candidate_id=candidate['id'], trigger_job_id=self.job_id, profile_id='fixture',
                reasons=[], created_at='2026-10-15T00:00:00Z', last_seen_at='2026-10-15T00:00:00Z',
                state='QUEUED', domain_evidence=[], source_snapshot={}, operation_id=operation_id))
        context = self.resolve(operation_id)
        self.assertEqual(context['company_id'], source['company_id'])
        self.assertEqual(context['source_id'], source['id'])
        self.assertEqual(context['job_id'], self.job_id)
        self.assertEqual(context['posting_id'], self.posting['id'])
        self.assert_private_absent(context)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == 'request-1').values(profile_id=None))
        self.assertEqual(self.resolve(operation_id), {})

    def test_multiple_companies_on_one_collector_do_not_choose_a_company(self):
        source_id = configure_source(self.store, 'batch-context', provider='remoteok', jitter_seconds=0)
        with self.store.engine.begin() as connection:
            original = dict(connection.execute(select(m.company_sources)).mappings().one())
            connection.execute(m.companies.insert().values(id='other-company', name='CANARY',
                                                         domain='https://private.test/'))
            connection.execute(m.company_sources.insert().values(
                **{**original, 'id': 'other-company-source', 'company_id': 'other-company',
                   'tenant': 'other-tenant', 'collector_source_id': source_id}))
            connection.execute(update(m.company_sources).where(
                m.company_sources.c.id == original['id']).values(collector_source_id=source_id))
        operation_id = enqueue_operation(self.store, 'DISCOVER', 'fixture', {'source_id': source_id})
        context = self.resolve(operation_id)
        self.assertEqual(context['source_id'], source_id)
        self.assertNotIn('company_id', context)
        self.assertNotIn('job_id', context)
        self.assertNotIn('posting_id', context)

    def test_comparison_context_respects_application_owner_and_ai_extraction_have_no_job(self):
        application_id = prepare(self.store, self.job_id, 'fixture', self.root)
        comparison = enqueue_operation(self.store, 'UPLOADED_RESUME_COMPARE_QUEUED', application_id, {
            'profile_id': 'fixture', 'application_id': application_id, 'request_id': 'comparison'})
        self.assertEqual(self.resolve(comparison)['posting_id'], self.posting['id'])
        invalid = enqueue_operation(self.store, 'UPLOADED_RESUME_COMPARE_QUEUED', application_id, {
            'profile_id': 'other', 'application_id': application_id, 'request_id': 'comparison-other'})
        self.assertEqual(self.resolve(invalid), {})
        for kind in ('RESUME_EXTRACT_QUEUED', 'AI_EVALUATE_QUEUED'):
            operation_id = enqueue_operation(self.store, kind, 'fixture', {
                'job_id': self.job_id, 'application_id': application_id})
            self.assertEqual(self.resolve(operation_id), {})
