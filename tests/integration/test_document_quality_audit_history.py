"""Saved-resume keyword audits use validated immutable artifacts and target metadata."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE


class DocumentQualityAuditHistoryTests(unittest.TestCase):
    setUp = fixtures.DocumentProvenanceTests.setUp
    tearDown = fixtures.DocumentProvenanceTests.tearDown

    def prepare_selected(self, *, tailored=True, skills=None):
        with fixture_server() as origin:
            discover(self.store, origin, 'fixture')
        job = self.store.jobs('fixture')[0]
        target_skills = ['Python', 'SQL'] if skills is None else skills
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(
                skills=target_skills))
        options = {'tailor_resume': True, 'tailor_fact_ids': ['python']} if tailored else {}
        application_id = prepare(self.store, job['id'], 'fixture', self.root, **options)
        return application_id, job['id']

    def resume(self, application_id, *, include_files=True):
        rows = application_documents(
            self.store, application_id, self.root if include_files else None)
        return next(row for row in rows if row['kind'] == 'resume')

    def replace_context(self, resume, context):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.documents).where(m.documents.c.id == resume['id']).values(
                generation_context=context))

    def test_audit_reports_saved_output_after_explicit_selection_and_never_writes(self):
        application_id, job_id = self.prepare_selected()
        before = self.store.workspace_status()['fingerprint']
        with patch('services.documents.generate.prepare', side_effect=AssertionError('Readback cannot regenerate')):
            resume = self.resume(application_id)
        self.assertEqual(self.store.workspace_status()['fingerprint'], before)
        audit = resume['quality_audit']
        self.assertEqual(audit['status'], 'AVAILABLE')
        self.assertEqual(audit['present_terms'], ['Python'])
        self.assertEqual(audit['missing_terms'], ['SQL'])
        self.assertEqual(audit['term_count'], 2)
        self.assertEqual(audit['factual_validation'], 'VALIDATED_SAVED_SOURCES')
        self.assertIn('SQL', [fact['text'] for fact in self.store.profile('fixture')['facts']])
        self.assertNotIn('SQL', resume['preview_text'])
        self.assertEqual(audit['source']['document_id'], resume['id'])
        self.assertEqual(audit['source']['document_version'], resume['version'])
        self.assertEqual(audit['source']['profile_version'], resume['profile_version'])
        self.assertEqual(audit['source']['job_id'], job_id)
        self.assertEqual(audit['source']['target_fingerprint'],
                         resume['generation_context']['target_fingerprint'])
        saved_bytes = (self.root / application_id / resume['files']['txt']).read_bytes()
        self.assertEqual(audit['source']['text_sha256'], hashlib.sha256(saved_bytes).hexdigest())
        self.assertNotIn(PROFILE['email'], json.dumps(audit))
        self.assertNotIn(PROFILE['name'], json.dumps(audit))
        self.assertNotIn('preview_text', audit)

    def test_current_profile_and_job_edits_do_not_reinterpret_a_saved_revision(self):
        application_id, job_id = self.prepare_selected()
        original = self.resume(application_id)['quality_audit']
        changed = deepcopy(PROFILE)
        changed['name'] = 'Changed Fixture'
        changed['facts'][0]['text'] = 'Rust'
        self.store.save_profile(changed)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(
                title='Changed role', description='A new Rust posting.', skills=['Rust']))
        before = self.store.workspace_status()['fingerprint']
        self.assertEqual(self.resume(application_id)['quality_audit'], original)
        self.assertEqual(self.store.workspace_status()['fingerprint'], before)

    def test_unvalidated_legacy_and_empty_target_are_explicitly_unavailable(self):
        application_id, _ = self.prepare_selected(tailored=False)
        no_files = self.resume(application_id, include_files=False)['quality_audit']
        self.assertEqual(no_files['status'], 'UNAVAILABLE')
        self.assertEqual(no_files['reason'], 'DOCUMENT_NOT_VALIDATED')
        legacy = self.resume(application_id)['quality_audit']
        self.assertEqual(legacy['status'], 'UNAVAILABLE')
        self.assertEqual(legacy['reason'], 'TARGET_SNAPSHOT_MISSING')
        self.assertEqual(legacy['present_terms'], [])
        self.assertEqual(legacy['missing_terms'], [])
        self.assertEqual(legacy['term_count'], 0)

        prepare(self.store, self.store.application(application_id)['job_id'], 'fixture', self.root,
                tailor_resume=True, tailor_fact_ids=['python'])
        resume = self.resume(application_id)
        context = deepcopy(resume['generation_context'])
        context['job_skills'] = []
        self.replace_context(resume, context)
        empty = self.resume(application_id)['quality_audit']
        self.assertEqual(empty['status'], 'UNAVAILABLE')
        self.assertEqual(empty['reason'], 'NO_TARGET_TERMS')

    def test_corrupted_artifact_or_missing_source_fact_cannot_receive_an_available_audit(self):
        application_id, _ = self.prepare_selected()
        resume = self.resume(application_id)
        text_path = self.root / application_id / resume['files']['txt']
        original = text_path.read_bytes()
        text_path.write_bytes(original + b'\nUnexpected unsupported claim')
        corrupt = self.resume(application_id)
        self.assertIsNotNone(corrupt['preview_error'])
        self.assertEqual(corrupt['quality_audit']['reason'], 'DOCUMENT_NOT_VALIDATED')
        self.assertEqual(corrupt['quality_audit']['status'], 'UNAVAILABLE')
        text_path.write_bytes(original)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.facts).where(
                m.facts.c.profile_id == 'fixture', m.facts.c.version == resume['profile_version'],
                m.facts.c.fact_id == 'python').values(user_approved=False))
        unsupported = self.resume(application_id)
        self.assertIsNotNone(unsupported['preview_error'])
        self.assertEqual(unsupported['quality_audit']['status'], 'UNAVAILABLE')

    def test_invalid_target_context_and_overflow_fail_closed_without_breaking_history(self):
        application_id, _ = self.prepare_selected()
        resume = self.resume(application_id)
        original = deepcopy(resume['generation_context'])
        for changes, reason in [
                ({'job_id': 'different-job'}, 'TARGET_SNAPSHOT_INVALID'),
                ({'target_fingerprint': 'not-a-snapshot'}, 'TARGET_SNAPSHOT_INVALID'),
                ({'job_skills': ['Python', 17]}, 'TARGET_SNAPSHOT_INVALID'),
                ({'job_skills': ['Python'] * 101}, 'AUDIT_INPUT_LIMIT_EXCEEDED')]:
            with self.subTest(changes=changes):
                self.replace_context(resume, dict(original, **changes))
                result = self.resume(application_id)
                self.assertIsNone(result['preview_error'])
                self.assertEqual(result['quality_audit']['status'], 'UNAVAILABLE')
                self.assertEqual(result['quality_audit']['reason'], reason)
                self.assertEqual(result['quality_audit']['present_terms'], [])
                self.assertEqual(result['quality_audit']['missing_terms'], [])

    def test_current_avoid_list_still_blocks_the_saved_resume_audit(self):
        application_id, _ = self.prepare_selected()
        self.assertEqual(self.resume(application_id)['quality_audit']['status'], 'AVAILABLE')
        self.store.save_profile(dict(PROFILE, preferences={'avoid_terms': ['Python']}))
        resume = self.resume(application_id)
        self.assertIsNotNone(resume['avoid_list_error'])
        self.assertIsNotNone(resume['preview_error'])
        self.assertEqual(resume['quality_audit']['status'], 'UNAVAILABLE')
        self.assertEqual(resume['quality_audit']['reason'], 'DOCUMENT_NOT_VALIDATED')

    def test_known_invalid_target_provenance_blocks_audit_even_with_valid_saved_bytes(self):
        application_id, job_id = self.prepare_selected()
        prepare(self.store, job_id, 'fixture', self.root, tailor_resume=True)
        resume = self.resume(application_id)
        self.assertIs(resume['responsibility_context_valid'], True)
        self.assertEqual(resume['quality_audit']['status'], 'AVAILABLE')
        context = deepcopy(resume['generation_context'])
        context['target_fingerprint'] = '0' * 64
        self.assertNotEqual(context['target_fingerprint'],
                            resume['generation_context']['target_fingerprint'])
        self.replace_context(resume, context)
        before = self.store.workspace_status()['fingerprint']
        result = self.resume(application_id)
        self.assertIsNone(result['preview_error'])
        self.assertEqual(result['preview_text'], resume['preview_text'])
        self.assertIs(result['responsibility_context_valid'], False)
        self.assertEqual(result['quality_audit']['status'], 'UNAVAILABLE')
        self.assertEqual(result['quality_audit']['reason'], 'TARGET_SNAPSHOT_INVALID')
        self.assertIsNone(result['quality_audit']['source'])
        self.assertEqual(result['quality_audit']['present_terms'], [])
        self.assertEqual(result['quality_audit']['missing_terms'], [])
        self.assertEqual(self.store.workspace_status()['fingerprint'], before)

    def test_existing_profile_scoped_api_exposes_audit_and_rejects_cross_profile_reads(self):
        application_id, _ = self.prepare_selected()
        self.store.save_profile(dict(PROFILE, id='other', name='Other Fixture'))
        with fixture_server() as origin, TestClient(create_app(
                self.store, origin, self.root, operation_mode='FIXTURE')) as client:
            before = self.store.workspace_status()['fingerprint']
            response = client.get(f'/profiles/fixture/applications/{application_id}/documents')
            self.assertEqual(response.status_code, 200)
            rows = response.json()['documents']
            resume = next(row for row in rows if row['kind'] == 'resume')
            self.assertEqual(resume['quality_audit']['status'], 'AVAILABLE')
            self.assertEqual(resume['quality_audit']['source']['document_id'], resume['id'])
            self.assertTrue(all('quality_audit' not in row for row in rows if row['kind'] != 'resume'))
            denied = client.get(f'/profiles/other/applications/{application_id}/documents')
            self.assertEqual(denied.status_code, 404)
            self.assertNotIn(resume['id'], denied.text)
            self.assertEqual(self.store.workspace_status()['fingerprint'], before)


if __name__ == '__main__':
    unittest.main()
