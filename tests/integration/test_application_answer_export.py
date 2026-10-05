"""Question-aware reusable answers and immutable, manually reviewed exports."""
import hashlib
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from sqlalchemy import select
from tests.integration.test_spine import PROFILE


class ApplicationAnswerExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(PROFILE)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        self.application_id = prepare(self.store, job['id'], 'fixture', self.root)
        self.export_url = f'/profiles/fixture/applications/{self.application_id}/answers/export'
        self.client = TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_substantive_answer_edit_resets_approval_and_survives_reload(self):
        profile = dict(PROFILE, answers=[{
            'id': 'years-experience', 'question': 'How many years of experience?',
            'text': 'Five years', 'source': 'User review', 'verified': True,
            'user_approved': True, 'confidence': 95,
        }])
        first = self.client.put('/profiles', json=profile)
        self.assertEqual(first.status_code, 200, first.text)
        old_version = first.json()['version']

        profile['answers'][0]['text'] = 'About six years'
        second = self.client.put('/profiles', json=profile)
        self.assertEqual(second.status_code, 200, second.text)
        saved = self.client.get('/profiles/fixture').json()['answers'][0]
        self.assertEqual(saved['text'], 'About six years')
        self.assertFalse(saved['verified'])
        self.assertFalse(saved['user_approved'])
        self.assertEqual(self.store.profile('fixture', old_version)['answers'][0]['text'], 'Five years')
        profile['answers'][0].update(verified=False, user_approved=False)
        stable = self.client.put('/profiles', json=profile)
        self.assertEqual(stable.status_code, 200, stable.text)
        self.assertEqual(stable.json()['version'], second.json()['version'])

    def test_answer_and_contact_edits_rebind_unchanged_match_snapshot(self):
        previous = self.client.get('/profiles/fixture').json()
        old_version = previous['version']
        with self.store.engine.connect() as connection:
            prior = connection.execute(select(m.matches).where(
                m.matches.c.profile_id == 'fixture',
                m.matches.c.profile_version == old_version).order_by(m.matches.c.job_id)).mappings().all()
        self.assertTrue(prior, 'fixture discovery must create a match snapshot')

        profile = dict(PROFILE, contact=dict(previous['contact']), answers=[{
            'id': 'new-question', 'question': 'How do you approach teamwork?',
            'text': 'I work collaboratively.', 'source': 'Synthetic test',
            'verified': False, 'user_approved': False, 'confidence': 100,
        }])
        profile['contact']['city'] = 'Synthetic City'
        saved = self.client.put('/profiles', json=profile)
        self.assertEqual(saved.status_code, 200, saved.text)
        new_version = saved.json()['version']
        self.assertNotEqual(old_version, new_version)

        with self.store.engine.connect() as connection:
            rebound = connection.execute(select(m.matches).where(
                m.matches.c.profile_id == 'fixture',
                m.matches.c.profile_version == new_version).order_by(m.matches.c.job_id)).mappings().all()
        self.assertEqual([
            (row['job_id'], row['score'], row['missing'], row['components']) for row in rebound
        ], [
            (row['job_id'], row['score'], row['missing'], row['components']) for row in prior
        ])

    def test_export_requires_explicit_review_and_exact_approved_answer(self):
        profile = dict(PROFILE, answers=[{
            'id': 'years-experience', 'question': 'How many years of experience?',
            'text': 'Five years', 'source': 'User review', 'verified': True,
            'user_approved': True, 'confidence': 95,
        }])
        self.assertEqual(self.client.put('/profiles', json=profile).status_code, 200)

        bank = self.client.get(f'/profiles/fixture/applications/{self.application_id}/answers')
        self.assertEqual(bank.status_code, 200)
        self.assertEqual(bank.json()['answers'][0]['question'], profile['answers'][0]['question'])
        self.assertEqual(self.client.get(f'/profiles/other/applications/{self.application_id}/answers').status_code, 404)

        base = {'question': 'How many years of experience?', 'answer_id': 'years-experience',
                'text': 'Five years'}
        self.assertEqual(self.client.post(self.export_url,
                         json={'profile_id':'fixture','items': [dict(base, reviewed=False)]}).status_code, 422)
        self.assertEqual(self.client.post(self.export_url,
                         json={'profile_id':'fixture','items': [dict(base, question='What is your favorite color?', reviewed=True)]}).status_code, 422)
        self.assertEqual(self.client.post(self.export_url,
                         json={'profile_id':'fixture','items': [dict(base, question='Are you authorized to work in the US?', reviewed=True)]}).status_code, 422)
        self.assertEqual(self.client.post(self.export_url,
                         json={'profile_id':'fixture','items': [dict(base, question='What are your salary expectations?', reviewed=True)]}).status_code, 422)
        self.assertEqual(self.client.post('/profiles/other/applications/'+self.application_id+'/answers/export',
                         json={'profile_id':'other','items': [dict(base, reviewed=True)]}).status_code, 404)
        self.assertEqual(self.client.post(self.export_url, json={'profile_id':'fixture','items':[
            {'question':'What do you want?', 'text':'', 'reviewed':True}]}).status_code, 422)

        response = self.client.post(self.export_url,
                                     json={'profile_id':'fixture','items': [dict(base, question='HOW MANY YEARS OF EXPERIENCE?', reviewed=True)]})
        self.assertEqual(response.status_code, 201, response.text)
        document_id = response.json()['document_id']
        exported = self.client.get(f'/documents/{document_id}/txt')
        self.assertEqual(exported.status_code, 200)
        self.assertIn('Five years', exported.text)
        documents = self.client.get(f'/applications/{self.application_id}/documents').json()['documents']
        row = next(item for item in documents if item['id'] == document_id)
        self.assertEqual(row['kind'], 'application-answer-export')
        self.assertEqual(row['sha256']['txt'], hashlib.sha256(exported.content).hexdigest())
        self.assertEqual(row['generation_context']['items'][0]['answer_id'], 'years-experience')
        self.assertEqual(row['profile_version'], self.client.get('/profiles/fixture').json()['version'])

    def test_unapproved_answer_cannot_be_reused(self):
        profile = dict(PROFILE, answers=[{
            'id': 'years-experience', 'question': 'How many years of experience?',
            'text': 'Five years', 'source': 'Manual entry', 'verified': False,
            'user_approved': False, 'confidence': 95,
        }])
        self.assertEqual(self.client.put('/profiles', json=profile).status_code, 200)
        response = self.client.post(self.export_url, json={'profile_id':'fixture','items':[{
            'question':'How many years of experience?', 'answer_id':'years-experience',
            'text':'Five years', 'reviewed':True,
        }]})
        self.assertEqual(response.status_code, 422)

    def test_sensitive_answer_variants_cannot_reuse_the_approved_bank(self):
        questions = [
            'What is your ethnic origin?',
            'What is your religious affiliation?',
            'Do you have disabilities?',
            'Have you ever been convicted of a crime?',
            'Are you authorized to work in the United States?',
            'Are you eligible to work in the United States?',
            'What is your salary expectation?',
        ]
        profile = dict(PROFILE, answers=[{
            'id': f'sensitive-{index}', 'question': question,
            'text': 'Fictional approved response', 'source': 'Synthetic test',
            'verified': True, 'user_approved': True, 'confidence': 100,
        } for index, question in enumerate(questions)])
        self.assertEqual(self.client.put('/profiles', json=profile).status_code, 200)

        for index, question in enumerate(questions):
            with self.subTest(question=question):
                response = self.client.post(self.export_url, json={
                    'profile_id': 'fixture', 'items': [{
                        'question': question, 'text': 'Fictional approved response',
                        'answer_id': f'sensitive-{index}', 'reviewed': True,
                    }],
                })
                self.assertEqual(response.status_code, 422, response.text)

    def test_concurrent_answer_exports_receive_distinct_immutable_versions(self):
        count = 4
        ready = Barrier(count)
        payload = {'profile_id': 'fixture', 'items': [{
            'question': 'What would you like us to know?',
            'text': 'I would like to discuss my systems experience.',
            'reviewed': True,
        }]}

        def export_once():
            client = TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root))
            try:
                ready.wait(timeout=10)
                return client.post(self.export_url, json=payload)
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=count) as pool:
            responses = list(pool.map(lambda _: export_once(), range(count)))

        self.assertTrue(all(response.status_code == 201 for response in responses),
                        [response.text for response in responses])
        self.assertEqual(len({response.json()['document_id'] for response in responses}), count)
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.kind == 'application-answer-export').order_by(m.documents.c.version)).mappings().all()
        self.assertEqual([row['version'] for row in rows], list(range(1, count + 1)))

    def test_manual_answer_export_is_versioned_and_restart_readable(self):
        payload = {'items': [{'question': 'What would you like us to know?',
                              'text': 'I would like to discuss my systems experience.',
                              'reviewed': True}]}
        payload['profile_id'] = 'fixture'
        first = self.client.post(self.export_url, json=payload)
        second = self.client.post(self.export_url, json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertNotEqual(first.json()['document_id'], second.json()['document_id'])
        restarted = Store(self.store.url)
        try:
            with restarted.engine.connect() as connection:
                rows = connection.execute(select(m.documents).where(
                    m.documents.c.application_id == self.application_id,
                    m.documents.c.kind == 'application-answer-export').order_by(m.documents.c.version)).mappings().all()
            self.assertEqual([row['version'] for row in rows], [1, 2])
            self.assertNotEqual(rows[0]['sha256'], {})
        finally:
            restarted.engine.dispose()
