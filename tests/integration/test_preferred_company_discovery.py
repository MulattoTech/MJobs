"""Explicit preference drives existing durable discovery, without guessed origins."""
from copy import deepcopy
from pathlib import Path
import tempfile
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.company_preferences import create_company_preferences_router
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.company_discovery.requests import list_discovery_requests
from services.company_discovery.domains import record_company_domain
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.greenhouse import Greenhouse
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class PreferredCompanyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'preferred.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        profile = deepcopy(PROFILE)
        profile['facts'] = []
        profile.setdefault('preferences', {})['company_discovery'] = {
            'minimum_match_for_discovery': 100, 'priority_match_for_discovery': 100}
        self.store.save_profile(profile)
        self.company_id = identity('greenhouse', 'example')
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=self.company_id, name='Existing Employer', domain=''))
        source_id = configure_source(self.store, 'example')
        def board(request):
            return httpx.Response(200, json={'jobs': [RAW], 'meta': {'total': 1}}
                if request.url.path.endswith('/jobs') else {'name': 'Existing Employer'})
        collect_source(self.store, source_id, 'fixture', transport=httpx.MockTransport(board))
        self.assertEqual(list_discovery_requests(self.store), [])
        with self.store.engine.connect() as connection:
            self.job_id = connection.execute(select(m.jobs.c.id)).scalar_one()
        app = FastAPI()
        app.include_router(create_company_preferences_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = f'/profiles/fixture/company-preferences/{self.company_id}'

    def prefer(self, request_id='prefer-1', **overrides):
        return self.client.put(self.url, json={
            'status': 'PREFERRED', 'request_id': request_id, 'job_id': self.job_id, **overrides})

    def test_known_low_match_preference_creates_durable_review_once(self):
        response = self.prefer()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['discovery']['status'], 'NEEDS_ORIGIN_REVIEW')
        requests = list_discovery_requests(self.store)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['reasons'], ['USER_PREFERRED_COMPANY'])
        self.assertIsNone(requests[0]['operation_id'])
        self.assertTrue(self.prefer().json()['replayed'])
        self.assertEqual(len(list_discovery_requests(self.store)), 1)
        reopened = Store(self.store.url)
        try:
            self.assertEqual(list_discovery_requests(reopened)[0]['id'], requests[0]['id'])
        finally:
            reopened.engine.dispose()

    def test_conflicting_replay_cannot_create_discovery(self):
        first = self.prefer(status='NEUTRAL')
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.prefer().status_code, 409)
        self.assertEqual(list_discovery_requests(self.store), [])

    def test_job_context_must_belong_to_selected_company(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(id='another', name='Another', domain=''))
        response = self.client.put('/profiles/fixture/company-preferences/another', json={
            'status': 'PREFERRED', 'request_id': 'wrong-company', 'job_id': self.job_id})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(list_discovery_requests(self.store), [])
        self.assertEqual(self.client.get('/profiles/fixture/company-preferences/another').json()['status'], 'NEUTRAL')

    def test_verified_preference_executes_enrollment_import_and_reload(self):
        record_company_domain(self.store, self.company_id, 'employer.example',
            'FIRST_PARTY_JOB_LINK', ['Controlled original posting evidence'])
        response = self.prefer()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['discovery']['status'], 'QUEUED')
        request = list_discovery_requests(self.store)[0]
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == request['operation_id'])).mappings().one()
        self.assertEqual(operation['params']['trigger_reason'], 'USER_PREFERRED_COMPANY')
        pages = {'/robots.txt': 'User-agent: *\nAllow: /',
            '/': '<a rel="careers" href="/careers">Careers</a>',
            '/careers': '<script src="https://boards.greenhouse.io/embed/job_board/js?for=preferred"></script>'}
        def page(client, url, deadline):
            path = httpx.URL(url).path
            self.assertIn(path, pages)
            return httpx.Response(200, text=pages[path], request=httpx.Request('GET', url))
        def board(adapter, suffix, params=None):
            return {'jobs': [dict(RAW, id=9876, absolute_url='https://job-boards.greenhouse.io/preferred/jobs/9876')]}
        env = {'MJOBS_DATABASE_URL': self.store.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
            'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0', 'MJOBS_OUTPUT_DIR': str(Path(self.temp.name) / 'outputs')}
        with patch.dict(os.environ, env), patch.object(PublicCompanyClient, '_request_once', page), \
                patch.object(Greenhouse, '_get', board):
            result = execute_operation(request['operation_id'])
        self.assertEqual(result['status'], 'COMPLETED', result)
        reopened = Store(self.store.url)
        try:
            with reopened.engine.connect() as connection:
                sources = connection.execute(select(m.company_sources).where(
                    m.company_sources.c.tenant == 'preferred')).mappings().all()
                jobs = connection.execute(select(m.jobs).where(m.jobs.c.company_id == self.company_id)).mappings().all()
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]['company_id'], self.company_id)
            self.assertIsNotNone(sources[0]['last_success'])
            self.assertEqual(len(jobs), 2)
            self.assertEqual(list_discovery_requests(reopened)[0]['status'], 'COMPLETED')
        finally:
            reopened.engine.dispose()
        self.assertEqual(self.client.get(self.url).json()['discovery']['status'], 'COMPLETED')
        self.assertTrue(self.prefer().json()['replayed'])
        self.assertEqual(self.store.count('worker_operations'), 1)

    def test_disabled_policy_saves_preference_without_discovery(self):
        profile = deepcopy(PROFILE)
        profile['facts'] = []
        profile['preferences'] = {'company_discovery': {'company_discovery_enabled': False}}
        self.store.save_profile(profile)
        # Recalculate the current version so the selected job is current-visible.
        from services.discovery.matching import recalculate
        recalculate(self.store, 'fixture')
        response = self.prefer()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'PREFERRED')
        self.assertEqual(response.json()['discovery']['status'], 'NOT_QUEUED')
        self.assertEqual(list_discovery_requests(self.store), [])

    def test_committed_retry_survives_removed_match_and_new_invalid_job_is_rejected(self):
        self.assertEqual(self.prefer().status_code, 200)
        with self.store.engine.begin() as connection:
            connection.execute(m.matches.delete())
        retry = self.prefer()
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()['replayed'])
        self.assertEqual(self.prefer('new-intent', job_id='missing-job').status_code, 422)
        self.assertEqual(len(list_discovery_requests(self.store)), 1)

    def test_dead_lettered_discovery_remains_readable_and_replayable(self):
        record_company_domain(self.store, self.company_id, 'employer.example',
            'FIRST_PARTY_JOB_LINK', ['Controlled original posting evidence'])
        self.assertEqual(self.prefer().status_code, 200)
        operation_id = list_discovery_requests(self.store)[0]['operation_id']
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id)
                .values(state='DEAD_LETTERED', error='CONTROLLED_FAILURE'))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['discovery']['status'], 'DEAD_LETTERED')
        self.assertEqual(self.prefer().status_code, 200)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id)
                .values(state='DISCARDED'))
        self.assertEqual(self.client.get(self.url).json()['discovery']['status'], 'DISCARDED')
        self.assertEqual(self.prefer().status_code, 200)
        self.assertEqual(self.store.count('worker_operations'), 1)

    def test_dismissal_and_neutral_do_not_reopen_public_work(self):
        from services.company_discovery.requests import dismiss_discovery_request
        self.assertEqual(self.prefer().status_code, 200)
        request = list_discovery_requests(self.store)[0]
        dismiss_discovery_request(self.store, request['id'], request['revision'])
        self.assertEqual(self.prefer('neutral', status='NEUTRAL').status_code, 200)
        response = self.prefer('prefer-again')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['discovery']['status'], 'DISMISSED')
        self.assertEqual(self.store.count('worker_operations'), 0)

    def test_discovery_failure_rolls_back_preference_and_retry_can_recover(self):
        with patch('services.company_discovery.requests.record_discovery_request',
                side_effect=RuntimeError('Controlled transaction failure')):
            response = self.prefer()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get(self.url).json()['status'], 'NEUTRAL')
        self.assertEqual(self.prefer().status_code, 200)
        self.assertEqual(len(list_discovery_requests(self.store)), 1)

    def test_hourly_cap_and_other_profile_are_preserved(self):
        profile = deepcopy(PROFILE)
        profile['facts'] = []
        profile['preferences'] = {'company_discovery': {'max_new_company_discoveries_per_hour': 1}}
        self.store.save_profile(profile)
        self.assertEqual(self.prefer().status_code, 200)
        other_profile = deepcopy(PROFILE)
        other_profile.update(id='other', email='other@example.test')
        self.store.save_profile(other_profile)
        other_url = self.url.replace('/fixture/', '/other/')
        self.assertEqual(self.client.get(other_url).json()['status'], 'NEUTRAL')
        self.assertEqual(self.prefer('coalesced').status_code, 200)
        self.assertEqual(len(list_discovery_requests(self.store)), 1)
        source_id = configure_source(self.store, 'second-employer')
        raw = dict(RAW, absolute_url='https://job-boards.greenhouse.io/second-employer/jobs/1234')
        def board(request):
            return httpx.Response(200, json={'jobs': [raw]} if request.url.path.endswith('/jobs')
                else {'name': 'Second Employer'})
        collect_source(self.store, source_id, 'fixture', transport=httpx.MockTransport(board))
        second_company = identity('greenhouse', 'second-employer')
        with self.store.engine.connect() as connection:
            second_job = connection.execute(select(m.jobs.c.id).where(
                m.jobs.c.company_id == second_company)).scalar_one()
        response = self.client.put(f'/profiles/fixture/company-preferences/{second_company}', json={
            'status': 'PREFERRED', 'request_id': 'cap-blocked', 'job_id': second_job})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'PREFERRED')
        self.assertEqual(response.json()['discovery']['status'], 'NOT_QUEUED')
        self.assertEqual(len(list_discovery_requests(self.store)), 1)
