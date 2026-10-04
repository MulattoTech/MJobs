import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.company_discovery.domains import record_company_domain
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.monitoring import configure_monitoring, source_version, schedule_company_sources, queue_refresh
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class CompanySourceMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'monitor.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(copy.deepcopy(PROFILE))
        save_search_profile(self.store, dict(id='all', profile_id='fixture', name='All sources', source_ids=[]))
        self.company = identity('public-company-origin', 'https://employer.example')
        with self.store.engine.begin() as c:
            upsert(c, m.companies, dict(id=self.company, name='Employer', domain=''))
        self.source = upsert_company_job_source(self.store, company_id=self.company, provider='json_feed',
            tenant_identifier=self.company, career_url='https://employer.example/careers',
            public_api_url='https://employer.example/jobs.json', collection_strategy='PUBLIC_JSON',
            enabled=True, discovered_automatically=True, confidence=.45)
        self.env = dict(MJOBS_DATABASE_URL=self.store.url, MJOBS_OPERATION_MODE='PERSONAL',
                        MJOBS_REDIS_URL='redis://127.0.0.1:1/0', MJOBS_OUTPUT_DIR=str(self.root / 'out'))
        self.seen = []
        self.pages = {'/robots.txt':'User-agent: *\nAllow: /',
            '/':'<a rel="careers" href="/careers">Careers</a>',
            '/careers':'<link rel="alternate" type="application/feed+json" href="/jobs.json">',
            '/jobs.json':json.dumps({'version':'https://jsonfeed.org/version/1.1','items':[
                {'id':'one','title':'Python Engineer','content_text':'Python SQL', 'url':'https://employer.example/jobs/one'}]}),
            '/jobs/one':'<html>No structured identity</html>'}

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def fetch(self, url, deadline):
        self.seen.append(url)
        request = httpx.Request('GET', url)
        return httpx.Response(200, text=self.pages[request.url.path], request=request)

    def configure(self):
        return configure_monitoring(self.store, self.source['id'], source_version(self.source), 0, 3600)

    def monitor(self):
        with self.store.engine.connect() as c:
            return dict(c.execute(select(m.company_source_monitoring)).mappings().one())

    def test_legacy_enabled_flag_does_not_authorize_but_explicit_review_schedules_and_imports(self):
        self.assertEqual(schedule_company_sources(self.store)['scheduled'], [])
        self.assertEqual(self.store.count('company_source_monitoring'), 0)
        self.configure()
        scheduled = schedule_company_sources(self.store)['scheduled']
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(schedule_company_sources(self.store)['scheduled'], [])
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once', self.fetch):
            result = execute_operation(scheduled[0]['operation_id'])
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        self.assertEqual(self.monitor()['status'], 'HEALTHY')
        self.assertEqual(self.monitor()['completed_operation_id'], result['operation_id'])
        self.assertEqual(schedule_company_sources(self.store)['scheduled'], [])

    def test_verified_source_auto_enrolls_but_revocation_blocks_before_get(self):
        domain = record_company_domain(self.store, self.company, 'employer.example',
            'VERIFIED_EXISTING_DOMAIN', ['Independent employer record'])
        queued = schedule_company_sources(self.store)['scheduled'][0]
        with self.store.engine.begin() as c:
            c.execute(update(m.company_domains).where(m.company_domains.c.id == domain['id']).values(verified=False))
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, 'get', side_effect=AssertionError('revoked')):
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(self.monitor()['status'], 'BLOCKED')
        self.assertEqual(schedule_company_sources(self.store)['scheduled'], [])

    def test_cannot_review_unverified_origin_into_another_company(self):
        with self.store.engine.begin() as c:
            upsert(c, m.companies, dict(id='other', name='Other', domain=''))
            c.execute(update(m.company_sources).where(m.company_sources.c.id == self.source['id']).values(company_id='other', tenant='other'))
            source = dict(c.execute(select(m.company_sources)).mappings().one())
        with self.assertRaisesRegex(ValueError, 'IDENTITY'):
            configure_monitoring(self.store, source['id'], source_version(source), 0, 3600)
        self.assertEqual(self.store.count('company_source_monitoring'), 0)

    def test_changed_career_feed_queues_rediscovery_without_import_or_retarget(self):
        self.configure()
        queued = queue_refresh(self.store, self.source['id'], 'fixture')
        self.pages['/careers'] = '<link rel="alternate" type="application/feed+json" href="/changed.json">'
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once', self.fetch):
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(self.store.count('canonical_jobs'), 0)
        monitor = self.monitor()
        self.assertEqual(monitor['status'], 'REDISCOVERY_REQUIRED')
        self.assertIsNotNone(monitor['rediscovery_operation_id'])
        self.assertNotIn('https://employer.example/changed.json', self.seen)
        with self.store.engine.connect() as c:
            self.assertEqual(c.execute(select(m.company_sources.c.endpoint)).scalar_one(), self.source['endpoint'])

    def test_commit_receipt_prevents_crawl_replay_after_worker_crash(self):
        self.configure()
        queued = queue_refresh(self.store, self.source['id'], 'fixture')
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once', self.fetch):
            self.assertEqual(execute_operation(queued['operation_id'])['status'], 'COMPLETED')
        with self.store.engine.begin() as c:
            c.execute(update(m.operations).where(m.operations.c.id == queued['operation_id']).values(state='QUEUED'))
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, 'get', side_effect=AssertionError('committed operation replay')):
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(self.store.count('source_quality_observations'), 1)
