"""Durable per-fact rejection never changes original facts or their eligibility."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class FactRejectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='mjobs-fact-rejection-')))
        self.url = 'sqlite:///' + (self.root / 'facts.db').as_posix()
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.profile = deepcopy(PROFILE)
        self.profile['facts'].append(dict(id='incorrect', kind='experience',
            text='Incorrect synthetic extracted experience', source='Original synthetic resume version 1',
            verified=False, user_approved=False, confidence=90))
        self.store.save_profile(self.profile)
        self.client = self.enterContext(TestClient(create_app(self.store, 'http://127.0.0.1:1',
            self.root, 'redis://127.0.0.1:1/0', operation_mode='FIXTURE')))

    def view(self, profile_id='fixture'):
        response = self.client.get('/profiles/' + profile_id + '/fact-rejections')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def reject(self, fact_id='incorrect', *, profile_id='fixture', **changes):
        view = self.view(profile_id)
        fact = next(item for item in view['facts'] if item['fact_id'] == fact_id)
        body = dict(decision='REJECT', confirmed=True, expected_profile_version=view['profile_version'],
            fact_sha256=fact['fact_sha256'])
        return self.client.post('/profiles/' + profile_id + '/facts/' + fact_id + '/reject',
            json={**body, **changes})

    def test_explicit_rejection_is_append_only_idempotent_and_survives_reopen(self):
        original = self.store.profile('fixture')
        response = self.reject()
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        self.assertEqual(receipt['decision'], 'REJECT')
        self.assertEqual(self.store.profile('fixture'), original)
        self.assertEqual(self.reject().json(), receipt)
        view = self.view()
        rejected = next(item for item in view['facts'] if item['fact_id'] == 'incorrect')
        self.assertEqual(rejected['review_status'], 'REJECTED')
        self.assertEqual(rejected['decision_id'], receipt['decision_id'])
        with self.store.engine.connect() as connection:
            events = connection.execute(select(m.audit).where(m.audit.c.action == 'CAREER_FACT_REJECTED')).mappings().all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['payload']['fact_sha256'], rejected['fact_sha256'])
        reopened = Store(self.url)
        self.addCleanup(reopened.engine.dispose)
        with TestClient(create_app(reopened, 'http://127.0.0.1:1', self.root,
                'redis://127.0.0.1:1/0', operation_mode='FIXTURE')) as client:
            self.assertEqual(client.get('/profiles/fixture/fact-rejections').json(), view)
        self.assertEqual(reopened.profile('fixture'), original)

    def test_approved_fact_or_unconfirmed_choice_cannot_be_rejected(self):
        approved = self.reject('python')
        self.assertEqual(approved.status_code, 409)
        self.assertEqual(approved.json()['detail']['code'], 'FACT_REJECTION_REQUIRES_PENDING_FACT')
        for value in (False, 1, 'true', None):
            with self.subTest(confirmed=value):
                self.assertEqual(self.reject(confirmed=value).status_code, 422)
        self.assertEqual(self.reject(decision='REOPEN').status_code, 422)
        self.assertEqual(self.store.count('career_facts'), 3)
        self.assertEqual(next(row for row in self.view()['facts'] if row['fact_id'] == 'incorrect')['review_status'], 'PENDING')

    def test_wrong_version_or_content_hash_cannot_reject_current_fact(self):
        for changes in ({'expected_profile_version': '0' * 64}, {'fact_sha256': '0' * 64}):
            with self.subTest(changes=changes):
                result = self.reject(**changes)
                self.assertEqual(result.status_code, 409)
                self.assertIn(result.json()['detail']['code'],
                    {'FACT_REJECTION_VERSION_CHANGED', 'FACT_REJECTION_CONTENT_CHANGED'})
        self.assertEqual(next(row for row in self.view()['facts'] if row['fact_id'] == 'incorrect')['review_status'], 'PENDING')

    def test_profile_revision_does_not_inherit_old_rejection_and_preserves_old_audit(self):
        original = self.view()
        receipt = self.reject().json()
        changed = deepcopy(self.profile)
        changed['facts'][-1]['text'] = 'New synthetic extraction awaiting a new explicit decision'
        self.store.save_profile(changed, expected_version=original['profile_version'])
        stale = self.client.post('/profiles/fixture/facts/incorrect/reject', json=dict(
            decision='REJECT', confirmed=True, expected_profile_version=receipt['profile_version'],
            fact_sha256=receipt['fact_sha256']))
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['detail']['code'], 'FACT_REJECTION_VERSION_CHANGED')
        current = self.view()
        self.assertNotEqual(current['profile_version'], original['profile_version'])
        self.assertEqual(next(row for row in current['facts'] if row['fact_id'] == 'incorrect')['review_status'], 'PENDING')
        with self.store.engine.connect() as connection:
            retained = connection.execute(select(m.audit.c.payload).where(m.audit.c.id == receipt['decision_id'])).scalar_one()
        self.assertEqual(retained['profile_version'], original['profile_version'])
        self.assertEqual(retained['decision'], 'REJECT')

    def test_equal_fact_ids_in_other_profile_do_not_share_decisions(self):
        other = dict(deepcopy(self.profile), id='other', name='Other synthetic applicant')
        self.store.save_profile(other)
        self.assertEqual(self.reject().status_code, 200)
        self.assertEqual(next(row for row in self.view('other')['facts'] if row['fact_id'] == 'incorrect')['review_status'], 'PENDING')
        self.assertEqual(self.client.get('/profiles/missing/fact-rejections').status_code, 404)
        snapshot = self.view()
        self.assertEqual(self.client.post('/profiles/fixture/facts/missing/reject', json=dict(
            decision='REJECT', confirmed=True, expected_profile_version=snapshot['profile_version'],
            fact_sha256='0' * 64)).status_code, 404)
