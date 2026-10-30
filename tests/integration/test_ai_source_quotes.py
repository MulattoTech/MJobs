"""Actual durable adapter output is checked against the admitted public posting."""
import unittest
from unittest.mock import patch
from sqlalchemy import select, update
from packages.persistence import models as m
from tests.integration import test_profile_ai_credentials as credentials
from workers.tasks import execute_operation
from workers.operations import recover_expired


class AISourceQuoteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = credentials.ProfileAICredentialTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.store, self.client = self.fixture.store, self.fixture.client
        self.quotes = [{'field': 'description', 'quote': 'Python SQL synthetic private sentinel.'}]
        self.fixture.response_body['job_evidence'] = self.quotes

    def history(self):
        return self.client.get('/profiles/fixture/ai-cost-plan/evaluations').json()['runs']

    def test_real_worker_quotes_survive_reload_and_job_edit_without_recall(self):
        operation = self.fixture.queue()
        self.assertEqual(execute_operation(operation)['status'], 'COMPLETED')
        item = self.history()[0]['evaluations'][0]
        self.assertEqual(item['job_evidence'], {'status': 'VERIFIED_QUOTES', 'quotes': self.quotes})
        self.fixture.case.change_job()
        self.client = self.fixture.case.new_client()
        item = self.history()[0]['evaluations'][0]
        self.assertEqual(item['input_status'], 'STALE')
        self.assertEqual(item['job_evidence']['quotes'], self.quotes)
        self.assertEqual(execute_operation(operation)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertEqual(self.client.get('/profiles/other/ai-cost-plan/evaluations').json()['runs'], [])

    def test_invented_quote_blocks_publication_and_is_not_logged(self):
        self.fixture.response_body['job_evidence'] = [{'field': 'description', 'quote': 'Invented secret unsupported experience'}]
        result = execute_operation(self.fixture.queue())
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_ERROR')
        self.assertEqual(self.history(), [])
        self.assertNotIn('Invented secret', str(result))

    def test_legacy_result_remains_unknown(self):
        self.fixture.response_body.pop('job_evidence')
        self.assertEqual(execute_operation(self.fixture.queue())['status'], 'COMPLETED')
        self.assertEqual(self.history()[0]['evaluations'][0]['job_evidence'], {'status': 'UNKNOWN', 'quotes': []})

    def test_tampered_checkpoint_cannot_claim_verified_quotes(self):
        operation = self.fixture.queue()
        self.assertEqual(execute_operation(operation)['status'], 'COMPLETED')
        with self.store.engine.begin() as connection:
            states = connection.execute(select(m.ai_evaluation_batches.c.candidate_states).where(m.ai_evaluation_batches.c.id == operation)).scalar_one()
            key = next(iter(states))
            states[key]['result']['job_evidence'] = [{'field': 'title', 'quote': 'Unsupported forged source'}]
            connection.execute(update(m.ai_evaluation_batches).where(m.ai_evaluation_batches.c.id == operation).values(candidate_states=states))
        self.assertEqual(self.history()[0]['evaluations'][0]['job_evidence'], {'status': 'UNKNOWN', 'quotes': []})

    def test_staged_quote_response_recovers_without_a_second_provider_call(self):
        operation = self.fixture.queue()
        with patch('workers.tasks.complete_evaluation', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
                execute_operation(operation)
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertEqual(self.history(), [])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation).values(lease_expires_at='2000-01-01T00:00:00+00:00'))
        recover_expired(self.store)
        self.store.engine.dispose()
        self.assertEqual(execute_operation(operation)['status'], 'COMPLETED')
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertEqual(self.history()[0]['evaluations'][0]['job_evidence']['quotes'], self.quotes)

    def test_invalid_second_candidate_never_publishes_a_partial_batch(self):
        response = self.client.post('/jobs/manual', json={
            'request_id': 'quote-second-synthetic-job', 'title': 'Second Python engineer',
            'company': 'Synthetic Company', 'description': 'Python SQL synthetic private sentinel.',
            'reference_url': 'https://jobs.example.test/quote-second',
            'application_url': 'https://apply.example.test/quote-second', 'source_details': {},
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(self.fixture.preview()['candidates']), 2)
        def respond():
            if len(self.fixture.requests) == 2:
                self.fixture.response_body['job_evidence'] = [{'field': 'title', 'quote': 'Unsupported invented title'}]
        self.fixture.on_request = respond
        result = execute_operation(self.fixture.queue())
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_ERROR')
        self.assertEqual(len(self.fixture.requests), 2)
        self.assertEqual(self.history(), [])
