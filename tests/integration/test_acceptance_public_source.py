"""Controlled provider responses with real API, worker, SQLite and provenance.

No live source, RQ queue, browser or external application is exercised. The
existing selected synthetic application is prepared before the helper runs.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select

from apps.api.main import create_app
from cli.acceptance_public_source import run
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_spine import PROFILE
from tests.integration.test_wwr import RAW_WWR_RSS
from workers.tasks import execute_operation


class SameProfilePublicObservationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='mjobs-public-observation-')))
        self.url = 'sqlite:///' + (self.root / 'observation.db').as_posix()
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(deepcopy(PROFILE))
        self.origin = self.enterContext(fixture_server())
        discover(self.store, self.origin, 'fixture')
        self.job = next(job for job in self.store.jobs('fixture') if job['requisition'] == 'B')
        self.app_id = prepare(self.store, self.job['id'], 'fixture', self.root)
        self.enterContext(patch.dict(os.environ, {'MJOBS_DATABASE_URL': self.url,
            'MJOBS_OPERATION_MODE': 'FIXTURE', 'MJOBS_MOCK_ORIGIN': self.origin,
            'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0', 'MJOBS_OUTPUT_DIR': str(self.root)}))
        self.client = self.enterContext(TestClient(create_app(self.store, self.origin, self.root,
            operation_mode='FIXTURE')))
        self.baseline = dict(source={'commit': 'a' * 40}, profile_id='fixture', application_id=self.app_id,
            operation_id='trusted-ui-operation', request_id='trusted-ui-request',
            selected_application_job_id=self.job['id'],
            selected_application_route_id=self.store.application(self.app_id)['route_id'],
            external_submission_enabled=False)
        self.native_requests = []
        self.executed = set()

    def request(self, method, path, **kwargs):
        if method == 'GET' and path.startswith('/operations/'):
            operation_id = path.rsplit('/', 1)[1]
            if operation_id not in self.executed:
                self.executed.add(operation_id)
                execute_operation(operation_id)
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def boundary(self, _transport, request):
        self.assertEqual(request.method, 'GET')
        self.native_requests.append(str(request.url))
        if self.provider == 'wwr':
            self.assertEqual(str(request.url), 'https://weworkremotely.com/remote-jobs.rss')
            return httpx.Response(200, content=RAW_WWR_RSS)
        self.assertEqual(str(request.url), 'https://remotive.com/api/remote-jobs')
        return httpx.Response(200, json=RAW_REMOTIVE_PAYLOAD)

    def observe(self, provider):
        self.provider = provider
        before = {name: self.store.count(name) for name in
            ('career_profiles', 'applications', 'documents', 'application_attempts')}
        original_app = self.store.application(self.app_id)
        with patch('httpx.HTTPTransport.handle_request',
                   lambda transport, request: self.boundary(transport, request)):
            result = run('http://127.0.0.1:18700', self.baseline, self.root / 'public.json',
                acceptance_run_id='synthetic-campaign', provider=provider, request=self.request,
                execution_scope='CONTROLLED_TRANSPORT', containment_preflight=lambda *_: dict(
                    scheduler_contained=True, source_policy_exclusive=True, source_collection_exclusive=True,
                    owner='isolated-sqlite-test', token='test-only',
                    lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()))
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['acceptance_observations'][0]['status'], 'NOT_RUN')
        self.assertEqual(before, {name: self.store.count(name) for name in before})
        self.assertEqual(original_app, self.store.application(self.app_id))
        self.assertNotIn(self.job['id'], result['observed_real_job_ids'])
        self.assertEqual(len(self.native_requests), 1)
        with self.store.engine.connect() as connection:
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == result['source_id'])).mappings().one()
            self.assertFalse(source['enabled'])
        reopened = Store(self.url)
        self.addCleanup(reopened.engine.dispose)
        for job_id in result['observed_real_job_ids']:
            job = reopened.job(job_id, profile_id='fixture')
            self.assertFalse(job['attributes']['fictional'])
            self.assertTrue(job['postings'][0]['snapshots'])
            self.assertEqual(job['match']['profile_id'], 'fixture')
        return result

    def test_wwr_collection_preserves_application_and_persists_same_profile_jobs(self):
        self.observe('wwr')

    def test_remotive_collection_preserves_application_and_persists_same_profile_jobs(self):
        self.observe('remotive')
