"""Recorded job-scoring disclosures must match the real bounded prompt."""
import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

from services.ai.consumer import AIJobInput, _prompt
from services.ai.data_flow import JOB_SCORING_CONTRACT, public_job_scoring_data_flow
from services.ai.provider_credentials import public_access, snapshot_binding


class AIDataFlowDisclosureTests(unittest.TestCase):
    def binding(self, provider='openai', source='SAVED_PROFILE'):
        return {'provider': provider, 'source': source,
                'data_flow_contract': JOB_SCORING_CONTRACT}

    def test_new_plan_captures_marker_without_exposing_credential_material(self):
        store = Mock()
        store.encrypted_provider_credential.return_value = b'synthetic-encrypted-key'
        binding = snapshot_binding(store, 'fixture', 'openai')
        self.assertEqual(binding['data_flow_contract'], JOB_SCORING_CONTRACT)
        self.assertIn('ciphertext_fingerprint', binding)
        public = public_access(binding)
        self.assertEqual(public['data_flow']['status'], 'RECORDED')
        self.assertNotIn('ciphertext_fingerprint', json.dumps(public))
        self.assertNotIn('synthetic-encrypted-key', json.dumps(public))

    def test_legacy_and_unknown_markers_never_infer_today_configuration(self):
        with patch.dict(os.environ, {
                'MJOBS_OPENAI_BASE_URL': 'https://private-user:secret@host.invalid/path?token=secret',
                'OPENAI_API_KEY': 'synthetic-runtime-secret'}):
            for marker in (None, '', 'JOB_SCORING_V2', 1, True, [], {}):
                with self.subTest(marker=marker):
                    binding = self.binding(source='RUNTIME_CONFIGURATION')
                    binding['data_flow_contract'] = marker
                    self.assertEqual(public_access(binding)['data_flow'], {'status': 'UNKNOWN'})
                    self.assertIsNone(public_access(binding)['destination'])
        self.assertIsNone(public_access(None))

    def test_disclosure_is_a_closed_projection_not_reflected_snapshot_text(self):
        binding = self.binding()
        binding.update(prompt_fields=['secret-contact'], text_limits={'prompt': 999999},
                       local_records=['secret-session'], provider_retention='DELETED',
                       destination='https://secret.invalid', secret='synthetic-secret')
        before = copy.deepcopy(binding)
        report = public_job_scoring_data_flow(binding)
        self.assertEqual(report['prompt_fields'], [
            'job_title', 'company', 'job_description', 'deterministic_match_score'])
        self.assertEqual(report['provider_retention'], 'UNKNOWN')
        self.assertEqual(report['local_records'], ['evaluation_input_snapshot', 'advisory_results'])
        self.assertNotIn('secret', json.dumps(report))
        self.assertEqual(binding, before)
        report['prompt_fields'].append('injected')
        report['text_limits']['prompt'] = 999999
        fresh = public_job_scoring_data_flow(binding)
        self.assertNotIn('injected', fresh['prompt_fields'])
        self.assertEqual(fresh['text_limits']['prompt'], 12000)

    def test_runtime_service_name_never_establishes_a_local_destination(self):
        for provider in ('ollama', 'local', 'azure', 'azure-openai', 'openai-compatible', 'openai'):
            with self.subTest(provider=provider):
                access = public_access(self.binding(provider, 'RUNTIME_CONFIGURATION'))
                self.assertIsNone(access['destination'])
                self.assertEqual(access['data_flow']['status'], 'RECORDED')
                self.assertEqual(access['data_flow']['provider_retention'], 'UNKNOWN')

    def test_disabled_and_contradictory_bindings_cannot_claim_a_sent_projection(self):
        self.assertEqual(public_job_scoring_data_flow(self.binding('disabled', 'DISABLED')),
                         {'status': 'DISABLED', 'contract': JOB_SCORING_CONTRACT})
        for binding in (None, [], 'secret', self.binding('openai', 'DISABLED'),
                        self.binding('disabled', 'RUNTIME_CONFIGURATION'),
                        self.binding('local', 'SAVED_PROFILE'), self.binding('unknown')):
            with self.subTest(binding=binding):
                self.assertEqual(public_job_scoring_data_flow(binding), {'status': 'UNKNOWN'})

    def test_declared_limits_and_score_match_actual_prompt_projection(self):
        flow = public_job_scoring_data_flow(self.binding())
        limits = flow['text_limits']
        title = 'T' * limits['job_title']
        company = 'C' * limits['company']
        description = 'D' * limits['job_description'] + 'synthetic-omitted-private-tail'
        candidate = AIJobInput('private-job-id-not-sent', 73, title, company, description, True)
        prompt = _prompt(candidate)
        self.assertIn('Deterministic match score: 73/100', prompt)
        self.assertIn('Title: ' + title + '\nCompany: ' + company + '\n', prompt)
        self.assertIn('Untrusted job description begins:\n' + 'D' * limits['job_description'], prompt)
        self.assertNotIn('synthetic-omitted-private-tail', prompt)
        self.assertIn('[TRUNCATED:', prompt)
        self.assertNotIn(candidate.job_id, prompt)
        self.assertLessEqual(len(prompt), limits['prompt'])

    def test_sensitive_text_in_job_fields_is_not_misrepresented_as_redacted(self):
        prompt = _prompt(AIJobInput('j', 75, 'Engineer', 'Example',
                                   'Contact synthetic-sensitive@example.test about this job.'))
        self.assertIn('synthetic-sensitive@example.test', prompt)
        self.assertNotIn('redacted', json.dumps(public_job_scoring_data_flow(self.binding())).lower())
