"""Real worker/application boundaries retain finite categories through API reopen."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from playwright.sync_api import Locator
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications import mock_adapter
from services.applications.errors import application_failure_category
from services.applications.submission_admission import authorize_submission
from services.applications.upload_integrity import snapshot_bytes
from services.discovery.pipeline import discover, MockClient
from services.discovery.sources.collect import configure_source
from services.discovery.sources.errors import SourceError
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation
from workers.tasks import execute_operation


class StructuredFailureTests(unittest.TestCase):
    def _new_store(self):
        self.database_url = 'sqlite:///' + (self.root / 'failures.db').as_posix()
        store = Store(self.database_url)
        store.migrate()
        return store

    def setUp(self):
        self.cleanup = ExitStack()
        self.addCleanup(self.cleanup.close)
        self.root = Path(self.cleanup.enter_context(tempfile.TemporaryDirectory()))
        self.origin = self.cleanup.enter_context(fixture_server())
        self.store = self._new_store()
        self.url = self.database_url
        self.cleanup.callback(self.store.engine.dispose)
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, 'id': 'other'})
        discover(self.store, self.origin, 'fixture')
        self.job_id = next(row['id'] for row in self.store.jobs() if row['requisition'] == 'B')
        self.app_id = prepare(self.store, self.job_id, 'fixture', self.root)
        self.cleanup.enter_context(patch.dict(os.environ, {
            'MJOBS_DATABASE_URL': self.url, 'MJOBS_OPERATION_MODE': 'FIXTURE',
            'MJOBS_OUTPUT_DIR': str(self.root), 'MJOBS_MOCK_ORIGIN': self.origin,
            'MJOBS_OPERATION_TIMEOUT': '60', 'MJOBS_BROWSER_DEBUG_CAPTURE': '0',
        }))

    def queue_apply(self, *, submit=False, task_key=None):
        return enqueue_operation(self.store, 'APPLY_MOCK', self.app_id, {'submit': submit},
                                 task_key=task_key)

    def set_route(self, suffix):
        application = self.store.application(self.app_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == application['route_id'])
                .values(url=self.origin + '/ats/apply/B' + suffix))

    def close_job(self, status='CLOSED'):
        with self.store.engine.begin() as connection:
            attributes = connection.execute(select(m.jobs.c.attributes).where(
                m.jobs.c.id == self.job_id)).scalar_one()
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.job_id)
                               .values(attributes={**attributes, 'status': status}))

    def assert_category(self, operation_id, result, category, *, no_retry=True):
        self.assertEqual(result['failure']['category'], category, result)
        reopened = Store(self.url)
        try:
            with reopened.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one()
                audits = list(connection.execute(select(m.audit.c.payload).where(
                    m.audit.c.entity_id == operation_id,
                    m.audit.c.action.in_(('OPERATION_ATTEMPT_FINISHED', 'OPERATION_DEAD_LETTERED',
                                         'OPERATION_RETRY_SCHEDULED')))).scalars())
            self.assertEqual(row['recovery_metadata'].get('error_category'), category)
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]['error_category'], category)
            self.assertEqual(row['error'], result['failure']['code'])
            if no_retry:
                self.assertIsNone(row['next_attempt_at'])
                self.assertEqual(row['retry_count'], 0)
            with TestClient(create_app(reopened, self.origin, self.root, operation_mode='FIXTURE')) as client:
                detail = client.get('/operations/' + operation_id)
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()['error_category'], category)
                progress = client.get('/profiles/fixture/operations/progress').json()['operations']
                self.assertEqual(next(item for item in progress if item['operation_id'] == operation_id)
                                 ['error_category'], category)
                foreign = client.get('/profiles/other/operations/progress').json()['operations']
                self.assertFalse(any(item['operation_id'] == operation_id for item in foreign))
            self.assertNotIn('PRIVATE_FAILURE_CANARY', json.dumps([result, dict(row), audits]))
        finally:
            reopened.engine.dispose()

    def test_source_categories_keep_existing_retry_policy_and_reopen(self):
        cases = [('SOURCE_NETWORK_ERROR', 'NETWORK_ERROR', True),
                 ('SOURCE_RATE_LIMITED', 'RATE_LIMITED', True),
                 ('SOURCE_CHANGED', 'SOURCE_CHANGED', False),
                 ('SOURCE_HTTP_403', 'SOURCE_BLOCKED', False),
                 ('SOURCE_HTTP_401', 'AUTH_REQUIRED', False)]
        for code, category, retry in cases:
            with self.subTest(category=category):
                source = configure_source(self.store, 'structured-' + code.lower())
                operation = enqueue_operation(self.store, 'DISCOVER', 'fixture', {'source_id': source})
                class Adapter:
                    def search(self):
                        raise SourceError(code)
                with patch('services.discovery.sources.collect.source_adapter', return_value=Adapter()):
                    result = execute_operation(operation)
                self.assert_category(operation, result, category, no_retry=not retry)
                self.assertEqual(result['status'], 'QUEUED' if retry else 'DEAD_LETTERED')

    def test_explicit_captcha_and_auth_are_distinct_durable_categories(self):
        for suffix, category in [('?challenge=true', 'CAPTCHA_REQUIRED'),
                                  ('?auth_expired=true', 'AUTH_REQUIRED')]:
            with self.subTest(category=category):
                # A separate explicit application request may start after review;
                # never recover/replay a submitted attempt in this fixture.
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.applications).where(m.applications.c.id == self.app_id)
                                       .values(state='PREPARED'))
                self.set_route(suffix)
                operation = self.queue_apply(task_key='explicit-' + category)
                result = execute_operation(operation)
                self.assert_category(operation, result, category)
                self.assertIsNone(self.store.application(self.app_id)['confirmation'])

    def test_unknown_dynamic_field_retains_exact_category(self):
        self.set_route('?dynamic=unknown')
        operation = self.queue_apply()
        self.assert_category(operation, execute_operation(operation), 'FIELD_UNKNOWN')

    def test_changed_questionnaire_blocks_before_browser(self):
        first = httpx.get(self.origin + '/ats/questions/B', timeout=5).json()
        changed = {**first, 'revision': 2}
        operation = self.queue_apply()
        with patch.object(MockClient, 'get', side_effect=[httpx.Response(200, json=first),
                httpx.Response(200, json=changed)]), patch.object(mock_adapter, 'sync_playwright') as browser:
            result = execute_operation(operation)
        browser.assert_not_called()
        self.assert_category(operation, result, 'FORM_CHANGED')

    def test_optional_category_read_failure_preserves_blocked_outcome(self):
        questionnaire = httpx.get(self.origin + '/ats/questions/B', timeout=5).json()
        questionnaire['questions'].append({
            'id': 'unmapped', 'label': 'Unknown required field', 'required': True,
            'requirements': {},
        })
        operation = self.queue_apply()
        with patch.object(mock_adapter, '_read_questionnaire', return_value=questionnaire), \
                patch('services.applications.errors.select', side_effect=RuntimeError('unavailable')):
            result = execute_operation(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['failure'], {'code': 'REVIEW_REQUIRED', 'category': None})
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == operation)).mappings().one()
        self.assertEqual(row['state'], 'BLOCKED')
        self.assertIsNone(row['next_attempt_at'])
        self.assertEqual(row['retry_count'], 0)

    def test_upload_integrity_failure_is_structured_and_never_retried(self):
        operation = self.queue_apply()
        def changed_bytes(*args, **kwargs):
            return snapshot_bytes('resume', 'synthetic-document', 'resume.pdf', 'application/pdf',
                                  b'%PDF-PRIVATE_FAILURE_CANARY', 'a' * 64)
        with patch('services.applications.upload_integrity.snapshot_prepared', side_effect=changed_bytes):
            result = execute_operation(operation)
        self.assert_category(operation, result, 'UPLOAD_FAILED')
        self.assertEqual(self.store.count('application_attempts'), 0)

    def test_native_form_validation_failure_keeps_fixed_category(self):
        original = Locator.evaluate
        def invalidate(locator, expression, *args, **kwargs):
            if expression == 'form => form.checkValidity()':
                original(locator, "form => form.querySelector('input').setCustomValidity('PRIVATE_FAILURE_CANARY')")
            return original(locator, expression, *args, **kwargs)
        operation = self.queue_apply()
        with patch.object(Locator, 'evaluate', invalidate):
            result = execute_operation(operation)
        self.assert_category(operation, result, 'VALIDATION_FAILED')
        self.assertEqual(httpx.get(self.origin + '/_test/request-counts', timeout=5).json()['submit'], 0)

    def test_closed_job_blocks_preparation_browser_and_final_admission(self):
        for status in ('REMOVED', 'EXPIRED', 'FILLED', 'CLOSED'):
            with self.subTest(status=status):
                self.close_job(status)
                before = self.store.count('documents')
                operation = enqueue_operation(self.store, 'PREPARE', 'fixture', {'job_id': self.job_id},
                                              task_key='closed-prepare-' + status)
                self.assert_category(operation, execute_operation(operation), 'JOB_CLOSED')
                self.assertEqual(self.store.count('documents'), before)
                operation = self.queue_apply(task_key='closed-apply-' + status)
                with patch.object(mock_adapter, 'sync_playwright') as browser, \
                        patch.object(mock_adapter, '_read_questionnaire') as questionnaire:
                    result = execute_operation(operation)
                browser.assert_not_called()
                questionnaire.assert_not_called()
                self.assert_category(operation, result, 'JOB_CLOSED')
                decision = authorize_submission(self.store, self.app_id, 100)
                self.assertEqual(decision.reasons, ('JOB_CLOSED',))
                self.assertFalse(decision.allowed)

    def test_closure_after_browser_start_blocks_final_submit_and_is_classified(self):
        authorize = mock_adapter._authorize
        def close_before_admission(*args, **kwargs):
            self.close_job()
            return authorize(*args, **kwargs)
        operation = self.queue_apply(submit=True)
        with patch.object(mock_adapter, '_authorize', close_before_admission):
            result = execute_operation(operation)
        self.assert_category(operation, result, 'JOB_CLOSED')
        self.assertEqual(httpx.get(self.origin + '/_test/request-counts', timeout=5).json()['submit'], 0)

    def test_unconfirmed_receipt_reconciliation_survives_later_closure_without_replay(self):
        self.close_job()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(m.applications.c.id == self.app_id)
                               .values(state='SUBMISSION_UNCONFIRMED'))
        operation = self.queue_apply(submit=True)
        with patch.object(mock_adapter, 'sync_playwright') as browser:
            result = execute_operation(operation)
        browser.assert_not_called()
        self.assert_category(operation, result, 'SUBMISSION_UNCONFIRMED')
        self.assertEqual(httpx.get(self.origin + '/_test/request-counts', timeout=5).json()['submit'], 0)

    def test_document_failure_is_categorized_after_real_fact_validation(self):
        profile = self.store.profile('fixture')
        for fact in profile['facts']:
            fact['user_approved'] = False
        self.store.save_profile(profile, expected_version=profile['version'])
        operation = enqueue_operation(self.store, 'PREPARE', 'fixture', {'job_id': self.job_id})
        self.assert_category(operation, execute_operation(operation), 'DOCUMENT_ERROR')

    def test_unsupported_ats_route_is_rejected_by_real_form_review_worker(self):
        application = self.store.application(self.app_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == application['route_id']).values(
                provider='greenhouse', tenant='example', requisition='12345',
                url='https://boards.greenhouse.io/example/jobs/12345'))
            connection.execute(update(m.applications).where(m.applications.c.id == self.app_id)
                               .values(target='REAL'))
        with TestClient(create_app(self.store, self.origin, self.root, operation_mode='FIXTURE')) as client:
            queued = client.post(f'/profiles/fixture/applications/{self.app_id}/form-reviews',
                                 json={'request_id': 'unsupported-ats-after-admission'})
            self.assertEqual(queued.status_code, 202, queued.text)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == application['route_id'])
                               .values(url='https://unsupported.example.invalid/apply'))
        operation = queued.json()['operation_id']
        self.assert_category(operation, execute_operation(operation), 'ATS_UNSUPPORTED')

    def test_ambiguous_challenge_and_stale_preflight_do_not_invent_categories(self):
        application = self.store.application(self.app_id)
        self.assertIsNone(application_failure_category(self.store, {**application,
            'state': 'USER_INTERVENTION_REQUIRED', 'trace': ['CHALLENGE_DETECTED']}))
        with self.store.engine.begin() as connection:
            self.store._audit(connection, self.app_id, 'APPLICATION_QUESTION_PREFLIGHT', {
                'reason': 'UNMAPPED_REQUIRED_QUESTION', 'profile_version': 'stale',
                'document_version': application['document_version'], 'route_id': application['route_id']})
        self.assertIsNone(application_failure_category(self.store, {**application,
            'state': 'REVIEW_REQUIRED', 'trace': ['QUESTION_PREFLIGHT_UNKNOWN']}))
        with patch.object(self.store.engine, 'connect', side_effect=RuntimeError('unavailable')):
            self.assertIsNone(application_failure_category(self.store, {**application,
                'state': 'REVIEW_REQUIRED', 'trace': ['QUESTION_PREFLIGHT_UNKNOWN']}))


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'),
                     'Set MJOBS_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database')
class StructuredFailurePostgresTests(StructuredFailureTests):
    from tests.integration.test_lever_form_review_flow import LeverFormReviewPostgresFlowTests as _Fixture
    _new_store = _Fixture._new_store
    _drop_schema = _Fixture._drop_schema


if __name__ == '__main__':
    unittest.main()
