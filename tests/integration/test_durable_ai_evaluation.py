"""Durable advisory scoring must survive API loss without duplicate provider calls."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.ai.consumer import AIJobEvaluation
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE
from workers.operations import claim, recover_expired
from workers.tasks import execute_operation


class DurableAIEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mjobs-durable-ai-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.url = 'sqlite:///' + (self.root / 'test.db').as_posix()
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(copy.deepcopy(PROFILE))
        self.store.save_profile(dict(PROFILE, id='other', email='other@example.test'))
        self.client = self.new_client()
        response = self.client.post('/jobs/manual', json={
            'request_id': 'durable-ai-synthetic-job', 'title': 'Infrastructure engineer',
            'company': 'Synthetic Company', 'description': 'Python SQL synthetic private sentinel.',
            'reference_url': 'https://jobs.example.test/durable-ai',
            'application_url': 'https://apply.example.test/durable-ai', 'source_details': {},
        })
        self.assertEqual(response.status_code, 201, response.text)
        recalculate(self.store, 'fixture')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.matches).where(m.matches.c.profile_id == 'fixture').values(score=99))
        self.store.save_ai_task_settings('fixture', {'job_scoring': {'provider': 'ollama', 'model': 'fixture'}})
        self.calls = []
        calls = self.calls
        class Router:
            def __init__(self, configuration):
                pass
            def generate_structured(self, **kwargs):
                calls.append(kwargs)
                return AIJobEvaluation(category='STRONG', rationale='Synthetic advisory result.')
        self.router = Router

    def new_client(self):
        client = TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root,
                                      operation_mode='PERSONAL'))
        self.addCleanup(client.close)
        return client

    def queue(self):
        return self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued',
                                json={'request_id': 'durable-ai-intent-0001'})

    def run_worker(self, operation_id):
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL': self.url, 'MJOBS_OPERATION_MODE': 'PERSONAL'}), \
                patch('services.ai.durable_evaluation.AITaskRouter', self.router):
            return execute_operation(operation_id)

    def change_job(self):
        job_id = self.store.jobs(profile_id='fixture')[0]['id']
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(
                description='Changed posting: this is a different synthetic role.'))
        return job_id

    def test_changed_job_before_worker_blocks_without_provider_call(self):
        operation = self.queue().json()['operation_id']
        self.change_job()
        result = self.run_worker(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_EVALUATION_JOB_CHANGED')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_job_change_during_admission_rolls_back_operation(self):
        from services.ai.selected_search_candidates import build_inputs
        def changed_inputs(*args, **kwargs):
            result = build_inputs(*args, **kwargs)
            self.change_job()
            return result
        with patch('services.ai.selected_search_candidates.build_inputs', side_effect=changed_inputs):
            response = self.queue()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail']['code'], 'AI_EVALUATION_JOB_CHANGED')
        self.assertEqual(self.client.get('/profiles/fixture/ai-cost-plan/operations').json()['operations'], [])
        self.assertEqual(self.calls, [])

    def test_job_change_after_staging_is_fenced_at_publication(self):
        from services.ai.durable_evaluation import evaluate_for_operation
        operation = self.queue().json()['operation_id']
        def changed_result(*args, **kwargs):
            result = evaluate_for_operation(*args, **kwargs)
            self.change_job()
            return result
        with patch('workers.tasks.evaluate_for_operation', side_effect=changed_result):
            result = self.run_worker(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_EVALUATION_JOB_CHANGED')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_recovered_staged_response_is_not_reused_for_changed_job(self):
        operation = self.queue().json()['operation_id']
        with patch('workers.tasks.complete_evaluation', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
                self.run_worker(operation)
        self.change_job()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation).values(
                lease_expires_at='2000-01-01T00:00:00+00:00'))
        self.store.engine.dispose()
        recover_expired(self.store)
        result = self.run_worker(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_EVALUATION_JOB_CHANGED')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_legacy_evaluation_is_readable_but_cannot_authorize_documents(self):
        operation = self.queue().json()['operation_id']
        self.assertEqual(self.run_worker(operation)['status'], 'COMPLETED')
        job_id = self.store.jobs(profile_id='fixture')[0]['id']
        with self.store.engine.begin() as connection:
            connection.execute(update(m.ai_job_evaluations).where(
                m.ai_job_evaluations.c.run_id == operation).values(job_fingerprint=None))
        result = self.client.get('/profiles/fixture/ai-cost-plan/operations/' + operation).json()['result']
        self.assertEqual(result['evaluations'][0]['input_status'], 'UNKNOWN')
        response = self.client.post('/profiles/fixture/ai-cost-plan/evaluations/' + operation + '/review-documents',
                                    json={'job_ids': [job_id]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail']['code'], 'AI_EVALUATION_INPUT_UNKNOWN')
        self.assertEqual(self.store.count('ai_document_reviews'), 0)

    def test_polling_timestamp_does_not_invalidate_evaluation(self):
        operation = self.queue().json()['operation_id']
        self.assertEqual(self.run_worker(operation)['status'], 'COMPLETED')
        job = self.store.jobs(profile_id='fixture')[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(
                attributes={**job['attributes'], 'last_seen_at': '2030-01-01T00:00:00Z'}))
        response = self.client.post('/profiles/fixture/ai-cost-plan/evaluations/' + operation + '/review-documents',
                                    json={'job_ids': [job['id']]})
        self.assertEqual(response.status_code, 200, response.text)

    def test_current_review_generates_documents_once_through_worker(self):
        job_id = self.store.jobs(profile_id='fixture')[0]['id']
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(
                description='Responsibilities:\n- Build Python SQL services for this synthetic employer.',
                skills=['Python', 'SQL']))
        operation = self.queue().json()['operation_id']
        self.assertEqual(self.run_worker(operation)['status'], 'COMPLETED')
        reviewed = self.client.post('/profiles/fixture/ai-cost-plan/evaluations/' + operation + '/review-documents',
                                    json={'job_ids': [job_id]})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        response = self.client.post('/applications', json={
            'profile_id': 'fixture', 'job_id': job_id, 'ai_evaluation_run_id': operation,
            'request_id': 'reviewed-current-preparation'})
        self.assertEqual(response.status_code, 202, response.text)
        with patch.dict(os.environ, MJOBS_OUTPUT_DIR=str(self.root)):
            result = self.run_worker(response.json()['operation_id'])
            self.assertEqual(result['status'], 'COMPLETED', result)
            self.run_worker(response.json()['operation_id'])
        self.assertEqual(self.store.count('documents'), 2)
        self.assertEqual(self.store.applications('fixture')[0]['state'], 'PREPARED')
        self.assertEqual(len(self.calls), 1)

    def test_change_after_provider_text_cap_still_invalidates_input(self):
        job_id = self.store.jobs(profile_id='fixture')[0]['id']
        prefix = 'Windows Server. ' * 600
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(description=prefix + 'original'))
        operation = self.queue().json()['operation_id']
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(description=prefix + 'changed'))
        result = self.run_worker(operation)
        self.assertEqual(result['error_category'], 'AI_EVALUATION_JOB_CHANGED')
        self.assertEqual(self.calls, [])

    def test_changed_job_during_provider_call_never_publishes_result(self):
        operation = self.queue().json()['operation_id']
        change_job = self.change_job
        class EditingRouter(self.router):
            def generate_structured(self, **kwargs):
                change_job()
                return super().generate_structured(**kwargs)
        self.router = EditingRouter
        result = self.run_worker(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_EVALUATION_JOB_CHANGED')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_changed_job_history_is_stale_and_review_is_rejected(self):
        operation = self.queue().json()['operation_id']
        self.assertEqual(self.run_worker(operation)['status'], 'COMPLETED')
        history_url = '/profiles/fixture/ai-cost-plan/evaluations'
        before = self.client.get(history_url).json()['runs'][0]
        self.assertEqual(before['evaluations'][0]['input_status'], 'CURRENT')
        job_id = self.change_job()
        self.store.engine.dispose()
        self.client = self.new_client()
        after = self.client.get(history_url).json()['runs'][0]
        self.assertEqual(after['evaluations'][0]['input_status'], 'STALE')
        self.assertEqual(after['evaluations'][0]['rationale'], before['evaluations'][0]['rationale'])
        response = self.client.post(history_url + '/' + operation + '/review-documents',
                                    json={'job_ids': [job_id]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail']['code'], 'AI_EVALUATION_JOB_CHANGED')
        self.assertEqual(self.store.count('ai_document_reviews'), 0)
        self.assertEqual(self.client.get(history_url.replace('/fixture/', '/other/')).json()['runs'], [])

    def test_reviewed_ai_preparation_rechecks_job_in_worker(self):
        operation = self.queue().json()['operation_id']
        self.assertEqual(self.run_worker(operation)['status'], 'COMPLETED')
        job_id = self.store.jobs(profile_id='fixture')[0]['id']
        reviewed = self.client.post('/profiles/fixture/ai-cost-plan/evaluations/' + operation + '/review-documents',
                                    json={'job_ids': [job_id]})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        prepared = self.client.post('/applications', json={
            'profile_id': 'fixture', 'job_id': job_id, 'ai_evaluation_run_id': operation})
        self.assertEqual(prepared.status_code, 202, prepared.text)
        self.change_job()
        with patch.dict(os.environ, MJOBS_OUTPUT_DIR=str(self.root)):
            result = self.run_worker(prepared.json()['operation_id'])
        self.assertNotEqual(result['status'], 'COMPLETED')
        self.assertEqual(result['error_category'], 'AI_EVALUATION_JOB_CHANGED')
        self.assertEqual(self.store.count('documents'), 0)

    def test_admission_reload_replay_worker_and_history(self):
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        admitted = response.json()
        self.assertEqual(admitted['state'], 'QUEUED')
        self.assertEqual(self.calls, [])
        self.client.close()
        self.client = self.new_client()
        replay = self.queue()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()['operation_id'], admitted['operation_id'])
        self.assertTrue(replay.json()['replayed'])
        status_url = '/profiles/fixture/ai-cost-plan/operations/' + admitted['operation_id']
        self.assertEqual(self.client.get(status_url).json()['state'], 'QUEUED')
        self.assertEqual(self.client.get(status_url.replace('/fixture/', '/other/')).status_code, 404)
        listed = self.client.get('/profiles/fixture/ai-cost-plan/operations')
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()['operations'][0]['operation_id'], admitted['operation_id'])
        self.assertNotIn('private sentinel', listed.text)
        self.assertEqual(self.client.get('/profiles/other/ai-cost-plan/operations').json()['operations'], [])
        with self.store.engine.connect() as connection:
            params = connection.execute(select(m.operations.c.params).where(
                m.operations.c.id == admitted['operation_id'])).scalar_one()
        self.assertNotIn('private sentinel', str(params))
        self.assertEqual(self.run_worker(admitted['operation_id'])['status'], 'COMPLETED')
        result = self.client.get(status_url).json()
        self.assertEqual(result['state'], 'COMPLETED')
        self.assertEqual(result['result']['evaluations'][0]['category'], 'STRONG')
        self.assertEqual(len(self.calls), 1)
        self.run_worker(admitted['operation_id'])
        self.assertEqual(len(self.calls), 1)
        history = self.client.get('/profiles/fixture/ai-cost-plan/evaluations').json()['runs']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['run_id'], admitted['operation_id'])

    def test_original_intent_conflicts_after_policy_change(self):
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        self.store.save_ai_cost_policy('fixture', {'minimum_score': 90, 'max_input_candidates': 5000,
                                                  'max_ai_candidates': 75, 'max_document_candidates': 20})
        self.assertEqual(self.queue().status_code, 409)

    def test_saved_policy_selects_only_highest_candidate_and_leaves_deterministic_scores_unchanged(self):
        self.assertEqual(self.client.post('/jobs/manual', json={
            'request_id': 'durable-ai-second-job', 'title': 'Second synthetic role',
            'company': 'Synthetic Company', 'description': 'Windows Server.',
            'reference_url': 'https://jobs.example.test/second-ai',
            'application_url': 'https://apply.example.test/second-ai', 'source_details': {},
        }).status_code, 201)
        recalculate(self.store, 'fixture')
        with self.store.engine.begin() as connection:
            ids = sorted(connection.execute(select(m.matches.c.job_id).where(
                m.matches.c.profile_id == 'fixture')).scalars())
            for job_id, score in zip(ids, (99, 90)):
                connection.execute(update(m.matches).where(m.matches.c.profile_id == 'fixture',
                    m.matches.c.job_id == job_id).values(score=score))
                connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(
                    skills=['Python'] if score == 99 else ['Python', 'SQL', 'Kubernetes'],
                    attributes={'required_skills': ['Python', 'SQL'],
                                'nice_to_have_skills': ['Kubernetes']}))
        self.store.save_ai_cost_policy('fixture', {'minimum_score': 75, 'max_input_candidates': 20,
                                                  'max_ai_candidates': 1, 'max_document_candidates': 1})
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.run_worker(response.json()['operation_id'])['status'], 'COMPLETED')
        run = self.store.ai_evaluation_history('fixture')[0]
        self.assertEqual([row['job_id'] for row in run['evaluations']], [ids[0]])
        self.assertEqual(run['policy']['max_ai_candidates'], 1)
        self.assertEqual(len(self.calls), 1)
        with self.store.engine.connect() as connection:
            scores = dict(connection.execute(select(m.matches.c.job_id, m.matches.c.score).where(
                m.matches.c.profile_id == 'fixture')).all())
            self.assertEqual(scores, {ids[0]: 99, ids[1]: 90})
            self.assertEqual(connection.execute(select(m.documents.c.id)).all(), [])

    def test_disabled_provider_and_invalid_request_never_admit_work(self):
        invalid = self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued',
                                   json={'request_id': 'invalid'})
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.store.save_ai_task_settings('fixture', {'job_scoring': {'provider': 'disabled', 'model': None}})
        disabled = self.queue()
        self.assertEqual(disabled.status_code, 409, disabled.text)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.operations.c.id).where(
                m.operations.c.kind == 'AI_EVALUATE_QUEUED')).all(), [])

    def test_original_evaluate_and_cancel_interfaces_use_durable_admission(self):
        with patch('apps.api.main.AITaskRouter', self.router):
            response = self.client.post('/profiles/fixture/ai-cost-plan/evaluate',
                                        json={'request_id': 'original-api-ai-intent'})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.calls, [], 'The old interface must not retain a request-bound provider path')
        wrong = self.new_client().post('/profiles/other/ai-cost-plan/evaluations/original-api-ai-intent/cancel')
        self.assertFalse(wrong.json()['cancelled'])
        cancelled = self.new_client().post('/profiles/fixture/ai-cost-plan/evaluations/original-api-ai-intent/cancel')
        self.assertTrue(cancelled.json()['cancelled'])
        self.assertEqual(self.client.get('/profiles/fixture/ai-cost-plan/operations/' +
                         response.json()['operation_id']).json()['state'], 'CANCELLED')

    def test_stale_worker_cannot_publish_after_recovery_blocks_uncertain_call(self):
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()['operation_id']
        store = self.store
        class StaleRouter(self.router):
            def generate_structured(self, **kwargs):
                with store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                        lease_expires_at='2000-01-01T00:00:00+00:00'))
                recover_expired(store)
                return super().generate_structured(**kwargs)
        self.router = StaleRouter
        self.assertEqual(self.run_worker(operation_id)['status'], 'OWNERSHIP_EXPIRED')
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])
        self.assertEqual(self.client.get('/profiles/fixture/ai-cost-plan/operations/' + operation_id).json()['error'],
                         'AI_PROVIDER_OUTCOME_UNKNOWN')

    def test_cross_instance_queued_cancellation_persists(self):
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        other_client = self.new_client()
        cancelled = other_client.post('/profiles/fixture/ai-cost-plan/operations/' +
                                      response.json()['operation_id'] + '/cancel')
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()['state'], 'CANCELLED')
        self.run_worker(response.json()['operation_id'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_expired_started_provider_call_is_never_replayed(self):
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()['operation_id']
        claim(self.store, operation_id)
        with self.store.engine.begin() as connection:
            batch = connection.execute(select(m.ai_evaluation_batches).where(
                m.ai_evaluation_batches.c.id == operation_id)).mappings().one()
            states = dict(batch['candidate_states'])
            states[next(iter(states))] = {'state': 'CALL_STARTED'}
            connection.execute(update(m.ai_evaluation_batches).where(
                m.ai_evaluation_batches.c.id == operation_id).values(candidate_states=states))
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                lease_expires_at='2000-01-01T00:00:00+00:00'))
        recover_expired(self.store)
        self.run_worker(operation_id)
        status = self.client.get('/profiles/fixture/ai-cost-plan/operations/' + operation_id).json()
        self.assertEqual(status['state'], 'BLOCKED')
        self.assertEqual(status['error'], 'AI_PROVIDER_OUTCOME_UNKNOWN')
        self.assertEqual(self.calls, [])

    def test_result_publication_failure_rolls_back_completion_and_reuses_staged_response(self):
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()['operation_id']
        with patch('workers.tasks.complete_evaluation', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
                self.run_worker(operation_id)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one()
            self.assertEqual(row['state'], 'RUNNING')
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                lease_expires_at='2000-01-01T00:00:00+00:00'))
        recover_expired(self.store)
        self.assertEqual(self.run_worker(operation_id)['status'], 'COMPLETED')
        self.assertEqual(len(self.calls), 1, 'A staged response must never cause a second provider call')
        self.assertEqual(len(self.store.ai_evaluation_history('fixture')), 1)

    def test_cancel_during_provider_call_never_publishes_partial_history(self):
        from workers.operations import request_cancel
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()['operation_id']
        store = self.store
        class CancellingRouter(self.router):
            def generate_structured(self, **kwargs):
                request_cancel(store, operation_id)
                return super().generate_structured(**kwargs)
        self.router = CancellingRouter
        self.assertEqual(self.run_worker(operation_id)['status'], 'CANCELLED')
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])
        self.assertEqual(len(self.calls), 1)

    def test_profile_edit_during_provider_call_fences_whole_batch(self):
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        store = self.store
        class EditingRouter(self.router):
            def generate_structured(self, **kwargs):
                updated = copy.deepcopy(PROFILE)
                updated['name'] = 'Changed synthetic name'
                store.save_profile(updated)
                return super().generate_structured(**kwargs)
        self.router = EditingRouter
        result = self.run_worker(response.json()['operation_id'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_EVALUATION_PROFILE_CHANGED')
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_expired_before_provider_call_resumes_safely(self):
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()['operation_id']
        claim(self.store, operation_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                lease_expires_at='2000-01-01T00:00:00+00:00'))
        recover_expired(self.store)
        self.assertEqual(self.run_worker(operation_id)['status'], 'COMPLETED')
        self.assertEqual(len(self.calls), 1)

    def test_provider_errors_are_durable_sanitized_and_never_publish_history(self):
        from services.ai.providers import AIProviderError
        from services.ai.structured_output import StructuredOutputError
        for error in (AIProviderError('AI_PROVIDER_TIMEOUT'),
                      StructuredOutputError('AI_STRUCTURED_PROVIDER_FAILED')):
            with self.subTest(code=error.code):
                response = self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued',
                    json={'request_id': 'provider-error-' + error.code})
                self.assertEqual(response.status_code, 202, response.text)
                class FailingRouter(self.router):
                    def generate_structured(self, **kwargs):
                        raise error
                with patch('services.ai.durable_evaluation.AITaskRouter', FailingRouter), \
                        patch.dict(os.environ, {'MJOBS_DATABASE_URL': self.url, 'MJOBS_OPERATION_MODE': 'PERSONAL'}):
                    result = execute_operation(response.json()['operation_id'])
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertEqual(result['error_category'], error.code)
                self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_batch_deadline_discards_late_response_without_partial_publication(self):
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        ticks = [100.0]
        class LateRouter(self.router):
            def generate_structured(self, **kwargs):
                self_timeout = kwargs['timeout_seconds']
                self_assert = 0 < self_timeout <= 30
                if not self_assert:
                    raise AssertionError('Provider call lost its bounded timeout')
                result = super().generate_structured(**kwargs)
                ticks[0] += 121
                return result
        self.router = LateRouter
        with patch('services.ai.durable_evaluation.time.monotonic', side_effect=lambda: ticks[0]):
            result = self.run_worker(response.json()['operation_id'])
        self.assertNotEqual(result['status'], 'COMPLETED')
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_worker_concurrency_is_shared_and_does_not_reject_durable_admission(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.store.save_ai_task_settings('other', {'job_scoring': {'provider': 'ollama', 'model': 'fixture'}})
        recalculate(self.store, 'other')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.matches).where(m.matches.c.profile_id == 'other').values(score=99))
        first = self.queue()
        self.assertEqual(first.status_code, 202, first.text)
        second = self.client.post('/profiles/other/ai-cost-plan/evaluate/queued',
                                  json={'request_id': 'other-profile-ai-intent'})
        self.assertEqual(second.status_code, 202, second.text)
        started, release = threading.Event(), threading.Event()
        class BlockingRouter(self.router):
            def generate_structured(self, **kwargs):
                started.set()
                if not release.wait(5):
                    raise AssertionError('Synthetic provider not released')
                return super().generate_structured(**kwargs)
        self.router = BlockingRouter
        with ThreadPoolExecutor(max_workers=1) as executor:
            work = executor.submit(self.run_worker, first.json()['operation_id'])
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(self.run_worker(second.json()['operation_id'])['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
                self.assertEqual(self.calls, [])
            finally:
                release.set()
            self.assertEqual(work.result(timeout=5)['status'], 'COMPLETED')
        self.assertEqual(self.run_worker(second.json()['operation_id'])['status'], 'COMPLETED')
        self.assertEqual(len(self.calls), 2)

    def test_real_api_connection_close_does_not_cancel_admitted_evaluation(self):
        import http.client
        import threading
        import time
        import uvicorn
        from fixtures.browser_ports import bound_browser_socket
        sock = bound_browser_socket()
        server = uvicorn.Server(uvicorn.Config(create_app(self.store, 'http://127.0.0.1:1', self.root,
                                                        operation_mode='PERSONAL'), log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    self.fail('Synthetic API did not start')
                time.sleep(.02)
            connection = http.client.HTTPConnection('127.0.0.1', sock.getsockname()[1], timeout=3)
            connection.request('POST', '/profiles/fixture/ai-cost-plan/evaluate/queued',
                               body='{"request_id":"closed-http-ai-intent"}',
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            # Close without consuming the response body, then stop the API.
            connection.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()
            self.assertFalse(thread.is_alive())
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.kind == 'AI_EVALUATE_QUEUED')).mappings().one()
        self.assertEqual(operation['state'], 'QUEUED')
        self.assertFalse(operation['cancel_requested'])
        self.assertEqual(self.run_worker(operation['id'])['status'], 'COMPLETED')
        self.assertEqual(len(self.calls), 1)

    def test_cancel_closes_actual_controlled_ollama_transport(self):
        from services.ai.providers import OllamaProvider
        self._assert_cancel_closes_transport(OllamaProvider)

    def test_cancel_closes_actual_controlled_openai_transport(self):
        from services.ai.providers import OpenAIProvider
        self._assert_cancel_closes_transport(OpenAIProvider)

    def test_cancel_closes_actual_controlled_compatible_transport(self):
        from services.ai.providers import OpenAICompatibleProvider
        self._assert_cancel_closes_transport(OpenAICompatibleProvider)

    def test_cancel_closes_actual_controlled_azure_transport(self):
        from services.ai.providers import AzureOpenAIProvider
        self._assert_cancel_closes_transport(AzureOpenAIProvider)

    def test_cancel_closes_actual_controlled_local_transport(self):
        from services.ai.providers import LocalEndpointProvider
        self._assert_cancel_closes_transport(LocalEndpointProvider)

    def _assert_cancel_closes_transport(self, provider_type):
        import socket
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from services.ai.tasks import AITaskRouter
        started, closed, finish = threading.Event(), threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Length', '1000')
                self.end_headers()
                self.wfile.write(b'{')
                self.wfile.flush()
                started.set()
                self.connection.settimeout(3)
                try:
                    if self.connection.recv(1) == b'':
                        closed.set()
                except (ConnectionResetError, BrokenPipeError):
                    closed.set()
                except socket.timeout:
                    pass
                finish.wait(3)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        response = self.queue()
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()['operation_id']
        provider = provider_type(base_url=f'http://127.0.0.1:{server.server_port}')
        self.router = lambda configuration: AITaskRouter(configuration, provider_factory=lambda _name: provider)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                work = executor.submit(self.run_worker, operation_id)
                try:
                    self.assertTrue(started.wait(3), 'Controlled provider call did not start')
                    acknowledged = self.new_client().post('/profiles/fixture/ai-cost-plan/operations/' + operation_id + '/cancel')
                    self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
                    self.assertTrue(closed.wait(1), 'Cancellation did not close controlled transport promptly')
                finally:
                    finish.set()
                self.assertEqual(work.result(timeout=5)['status'], 'CANCELLED')
            self.assertEqual(self.store.ai_evaluation_history('fixture'), [])
            reopened = Store(self.url)
            self.addCleanup(reopened.engine.dispose)
            with reopened.engine.connect() as connection:
                persisted = connection.execute(select(m.operations).where(
                    m.operations.c.id == operation_id)).mappings().one()
                self.assertEqual(persisted['state'], 'CANCELLED')
            readback = self.new_client().get('/profiles/fixture/ai-cost-plan/operations/' + operation_id)
            self.assertEqual(readback.status_code, 200, readback.text)
            self.assertEqual(readback.json()['state'], 'CANCELLED')
        finally:
            finish.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
