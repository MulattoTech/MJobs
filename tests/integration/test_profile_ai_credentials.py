"""Saved profile keys reach the real durable router only through synthetic transport."""
import base64
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from services.ai.providers.openai import OpenAIProvider
from tests.integration import test_durable_ai_evaluation as contract
from workers.tasks import execute_operation
from workers.operations import claim, recover_expired


class ProfileAICredentialTests(unittest.TestCase):
    def setUp(self):
        self.case = contract.DurableAIEvaluationTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store, self.client = self.case.store, self.case.client
        self.key = base64.urlsafe_b64encode(b'c' * 32).decode()
        self.secret = 'synthetic-profile-credential-only'
        self.environment_secret = 'synthetic-environment-must-not-win'
        self.enterContext(patch.dict(os.environ, {
            'MJOBS_CREDENTIAL_ENCRYPTION_KEY': self.key,
            'MJOBS_DATABASE_URL': self.case.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
            'OPENAI_API_KEY': self.environment_secret,
            'MJOBS_OPENAI_BASE_URL': 'https://unapproved.example/v1',
        }))
        self.store.save_ai_task_settings('fixture', {'job_scoring': {'provider': 'openai', 'model': 'synthetic'}})
        self.save_key()
        self.requests = []
        self.on_request = lambda: None
        self.response_status = 200
        self.response_headers = {}
        self.response_body = {'category': 'REVIEW', 'rationale': 'Synthetic controlled transport response.'}
        original_init = OpenAIProvider.__init__

        def handle(request):
            self.requests.append(request)
            self.on_request()
            return httpx.Response(self.response_status, headers=self.response_headers, json={'choices': [{'message': {'content': json.dumps({
                **self.response_body})}}]})

        def initialize(provider, *args, **kwargs):
            kwargs['client_factory'] = lambda **options: httpx.Client(
                transport=httpx.MockTransport(handle), **options)
            original_init(provider, *args, **kwargs)

        # Intercept every real OpenAI adapter construction, including the old
        # environment path, so RED tests cannot send a request to any network.
        self.enterContext(patch.object(OpenAIProvider, '__init__', initialize))

    def save_key(self, secret=None, profile='fixture'):
        response = self.client.put(f'/profiles/{profile}/provider-credentials/openai',
                                   json={'secret': secret or self.secret})
        self.assertEqual(response.status_code, 200, response.text)

    def preview(self, profile='fixture'):
        response = self.client.get(f'/profiles/{profile}/ai-cost-plan')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def queue(self, request_id='profile-key-evaluation-0001', profile='fixture', preview=None):
        response = self.client.post(f'/profiles/{profile}/ai-cost-plan/evaluate/queued', json={
            'request_id': request_id, 'preview_fingerprint': (preview or self.preview(profile))['fingerprint']})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['operation_id']

    def test_saved_key_reaches_real_router_at_fixed_origin_and_stays_private(self):
        preview = self.preview()
        self.assertEqual(preview['provider_access']['credential_source'], 'SAVED_PROFILE')
        operation = self.queue(preview=preview)
        self.client = self.case.new_client()  # Recover the saved intent through another API instance.
        self.assertEqual(execute_operation(operation)['status'], 'COMPLETED')
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer ' + self.secret)
        history = self.client.get('/profiles/fixture/ai-cost-plan/evaluations').json()['runs']
        self.assertEqual(history[0]['provider_access'], preview['provider_access'])
        self.assertEqual(execute_operation(operation)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(len(self.requests), 1)
        with self.store.engine.connect() as connection:
            batch = connection.execute(select(m.ai_evaluation_batches)).mappings().one()
            metadata = json.dumps(batch['snapshot'])
        public = json.dumps(preview) + json.dumps(history) + self.client.get('/operations?profile_id=fixture').text
        self.assertNotIn('ciphertext_fingerprint', public)
        for secret in (self.secret, self.environment_secret, self.key):
            self.assertNotIn(secret, public + metadata)
            if self.store.engine.dialect.name == 'sqlite':
                self.assertNotIn(secret.encode(), (self.case.root / 'test.db').read_bytes())

    def test_rotation_before_worker_blocks_without_fallback_or_call(self):
        operation = self.queue()
        self.save_key('synthetic-rotated-profile-credential')
        result = execute_operation(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_ERROR')
        self.assertEqual(self.requests, [])

    def test_deletion_before_worker_blocks_without_environment_fallback(self):
        operation = self.queue()
        self.assertEqual(self.client.delete('/profiles/fixture/provider-credentials/openai').status_code, 200)
        result = execute_operation(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_ERROR')
        self.assertEqual(self.requests, [])

    def test_unavailable_encryption_key_fails_before_call_started(self):
        operation = self.queue()
        with patch.dict(os.environ, {'MJOBS_CREDENTIAL_ENCRYPTION_KEY': base64.urlsafe_b64encode(b'x' * 32).decode()}):
            result = execute_operation(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_ERROR')
        self.assertEqual(self.requests, [])
        with self.store.engine.connect() as connection:
            states = connection.execute(select(m.ai_evaluation_batches.c.candidate_states)).scalar_one()
        self.assertTrue(all(row['state'] == 'PENDING' for row in states.values()))

    def test_rotation_during_response_prevents_publication(self):
        response = self.client.post('/jobs/manual', json={
            'request_id': 'credential-second-candidate', 'title': 'Second synthetic role',
            'company': 'Synthetic Company', 'description': 'Python SQL',
            'reference_url': 'https://jobs.example.test/credential-second',
            'application_url': 'https://apply.example.test/credential-second', 'source_details': {}})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.preview()['plan']['ai_candidate_count'], 2)
        operation = self.queue()
        self.on_request = lambda: self.save_key('synthetic-rotation-during-response')
        result = execute_operation(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_ERROR')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_rotation_after_preview_requires_new_explicit_plan(self):
        preview = self.preview()
        self.save_key('synthetic-rotation-after-preview')
        response = self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued', json={
            'request_id': 'profile-key-stale-preview', 'preview_fingerprint': preview['fingerprint']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'AI_EVALUATION_PREVIEW_CHANGED')
        self.assertEqual(self.requests, [])

    def test_each_profile_uses_only_its_own_saved_key(self):
        self.save_key('synthetic-other-profile-key', profile='other')
        self.store.save_ai_task_settings('other', {'job_scoring': {'provider': 'openai', 'model': 'synthetic'}})
        for profile, secret in (('fixture', self.secret), ('other', 'synthetic-other-profile-key')):
            operation = self.queue('profile-isolation-intent', profile=profile)
            self.assertEqual(execute_operation(operation)['status'], 'COMPLETED')
            self.assertEqual(self.requests[-1].headers['authorization'], 'Bearer ' + secret)
            self.assertEqual(len(self.store.ai_evaluation_history(profile)), 1)

    def test_saved_key_is_never_forwarded_to_a_redirect(self):
        operation = self.queue()
        self.response_status = 302
        self.response_headers = {'Location': 'https://unapproved.example/receive'}
        result = execute_operation(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.host, 'api.openai.com')
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_rotation_after_staging_is_fenced_in_completion_transaction(self):
        from services.ai.durable_evaluation import evaluate_for_operation
        operation = self.queue()

        def rotate_after_staging(*args):
            result = evaluate_for_operation(*args)
            self.save_key('synthetic-rotation-after-staging')
            return result

        with patch('workers.tasks.evaluate_for_operation', side_effect=rotate_after_staging):
            result = execute_operation(operation)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_PROVIDER_CREDENTIAL_CHANGED')
        self.assertEqual(self.store.ai_evaluation_history('fixture'), [])

    def test_pending_worker_restart_keeps_binding_and_calls_once(self):
        operation = self.queue()
        claim(self.store, operation)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation).values(
                lease_expires_at='2000-01-01T00:00:00+00:00'))
        recover_expired(self.store)
        self.assertEqual(execute_operation(operation)['status'], 'COMPLETED')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].headers['authorization'], 'Bearer ' + self.secret)

    def test_runtime_admission_does_not_switch_to_a_later_saved_key(self):
        self.client.delete('/profiles/fixture/provider-credentials/openai')
        preview = self.preview()
        self.assertEqual(preview['provider_access']['credential_source'], 'RUNTIME_CONFIGURATION')
        operation = self.queue(preview=preview)
        self.save_key()
        self.assertEqual(execute_operation(operation)['status'], 'COMPLETED')
        self.assertEqual(self.requests[0].headers['authorization'], 'Bearer ' + self.environment_secret)
        self.assertEqual(self.requests[0].url.host, 'unapproved.example')  # Synthetic transport only.

    def test_disabled_provider_never_resolves_a_saved_secret(self):
        self.store.save_ai_task_settings('fixture', {'job_scoring': {'provider': 'disabled', 'model': None}})
        with patch('services.ai.provider_credentials.CredentialCipher.from_environment',
                   side_effect=AssertionError('Disabled provider attempted decryption')):
            preview = self.preview()
            self.assertEqual(preview['provider_access']['credential_source'], 'DISABLED')
            response = self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued', json={
                'request_id': 'disabled-saved-credential-intent'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'AI_PROVIDER_DISABLED')
        self.assertEqual(self.requests, [])

    def test_separate_worker_process_decrypts_only_the_bound_saved_key(self):
        operation = self.queue()
        code = '''import json,sys,httpx
from unittest.mock import patch
from services.ai.providers.openai import OpenAIProvider
from workers.tasks import execute_operation
requests=[]
original=OpenAIProvider.__init__
def handle(request):
 requests.append(request)
 return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'category':'REVIEW','rationale':'Synthetic child worker response.'})}}]})
def initialize(provider,*args,**kwargs):
 kwargs['client_factory']=lambda **options:httpx.Client(transport=httpx.MockTransport(handle),**options)
 original(provider,*args,**kwargs)
with patch.object(OpenAIProvider,'__init__',initialize):
 result=execute_operation(sys.argv[1])
print(json.dumps({'status':result['status'],'calls':len(requests),'correct_key':bool(requests) and requests[0].headers.get('authorization')=='Bearer synthetic-profile-credential-only','origin':requests[0].url.host if requests else None}))
'''
        child = subprocess.run([sys.executable, '-c', code, operation], capture_output=True,
                               text=True, timeout=45, env={**os.environ, 'PYTHONUTF8': '1'})
        self.assertEqual(child.returncode, 0, child.stderr[-1000:])
        self.assertEqual(json.loads(child.stdout.strip().splitlines()[-1]), {
            'status': 'COMPLETED', 'calls': 1, 'correct_key': True, 'origin': 'api.openai.com'})
        self.assertEqual(len(self.store.ai_evaluation_history('fixture')), 1)

    def test_rotation_during_admission_rolls_back_the_outbox(self):
        from services.ai.selected_search_candidates import build_inputs

        def rotate_after_snapshot(*args, **kwargs):
            result = build_inputs(*args, **kwargs)
            self.save_key('synthetic-admission-race')
            return result

        with patch('services.ai.selected_search_candidates.build_inputs', side_effect=rotate_after_snapshot):
            response = self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued', json={
                'request_id': 'profile-key-admission-race'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'AI_PROVIDER_CREDENTIAL_CHANGED')
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.operations.c.id)).all(), [])
        self.assertEqual(self.requests, [])

    def test_other_profiles_ciphertext_cannot_be_decrypted_as_this_owner(self):
        self.save_key('synthetic-other-scope-key', profile='other')
        encrypted = self.store.encrypted_provider_credential('other', 'openai')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.provider_credentials).where(
                m.provider_credentials.c.profile_id == 'fixture').values(encrypted_secret=encrypted))
        result = execute_operation(self.queue())
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['error_category'], 'AI_ERROR')
        self.assertEqual(self.requests, [])

    def test_legacy_snapshot_keeps_runtime_source_without_backfilling_a_key(self):
        from services.ai.durable_evaluation import _digest
        operation = self.queue()
        with self.store.engine.begin() as connection:
            snapshot = dict(connection.execute(select(m.ai_evaluation_batches.c.snapshot).where(
                m.ai_evaluation_batches.c.id == operation)).scalar_one())
            snapshot.pop('provider_binding')
            params = dict(connection.execute(select(m.operations.c.params).where(
                m.operations.c.id == operation)).scalar_one())
            params['snapshot_digest'] = _digest(snapshot)
            connection.execute(update(m.ai_evaluation_batches).where(
                m.ai_evaluation_batches.c.id == operation).values(snapshot=snapshot))
            connection.execute(update(m.operations).where(m.operations.c.id == operation).values(params=params))
        self.assertEqual(execute_operation(operation)['status'], 'COMPLETED')
        self.assertEqual(self.requests[0].headers['authorization'], 'Bearer ' + self.environment_secret)
        history = self.client.get('/profiles/fixture/ai-cost-plan/evaluations').json()['runs']
        self.assertIsNone(history[0]['provider_access'])
