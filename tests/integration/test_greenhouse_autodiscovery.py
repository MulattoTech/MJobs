"""Official-domain ATS enrollment through real workers and durable collection."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.domains import record_company_domain
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.triggers import queue_company_discovery
from services.discovery.scheduler import schedule_due_sources
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.greenhouse import Greenhouse
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class GreenhouseAutodiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.url = getattr(self, 'database_url', f"sqlite:///{(self.root / 'ats.db').as_posix()}")
        self.store = Store(self.url)
        if self.store.engine.dialect.name == 'sqlite':
            self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(PROFILE)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {'id': 'canonical-employer', 'name': 'Example Systems', 'domain': ''})
        record_company_domain(self.store, 'canonical-employer', 'employer.example',
                              'FIRST_PARTY_JOB_LINK', ['verified original posting'])
        save_search_profile(self.store, {'id': 'all-sources', 'profile_id': 'fixture',
                            'name': 'All employers', 'enabled': True, 'source_ids': []})
        self.env = {'MJOBS_DATABASE_URL': self.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
                    'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0', 'MJOBS_OUTPUT_DIR': str(self.root / 'outputs')}
        self.candidate = {'company_name': 'Untrusted display name', 'company_website': 'https://employer.example/'}
        self.markup = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=example"></script>'
        self.jobs = [{'id': 101, 'title': 'Python Engineer', 'content': '<p>Python remote role</p>',
                     'location': {'name': 'Remote'}, 'absolute_url': 'https://job-boards.greenhouse.io/example/jobs/101'}]
        self.requests = []

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def page(self, client, url, deadline):
        self.requests.append(url)
        pages = {'/robots.txt': 'User-agent: *\nAllow: /',
                 '/': '<a rel="careers" href="/careers">Careers</a>', '/careers': self.markup}
        request = httpx.Request('GET', url)
        return httpx.Response(200, text=pages[request.url.path], request=request)

    def board(self, adapter, suffix, params=None):
        self.requests.append('https://boards-api.greenhouse.io/v1/boards/' + adapter.tenant + suffix)
        return {'name': 'Provider display name'} if not suffix else {'jobs': self.jobs}

    def discover(self):
        queued = queue_company_discovery(self.store, self.candidate, 'USER_ADDED_COMPANY',
                                          profile_id='fixture', public=True)
        self.assertEqual(queued['status'], 'QUEUED', queued)
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once', lambda client, url, deadline: self.page(client, url, deadline)), \
                patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None: self.board(adapter, suffix, params)):
            result = execute_operation(queued['operation_id'])
        return result

    def test_verified_ats_enrollment_import_reload_and_recurring_collection(self):
        result = self.discover()
        self.assertEqual(result['status'], 'COMPLETED', result)
        with self.store.engine.connect() as connection:
            company = connection.execute(select(m.companies)).mappings().one()
            source = connection.execute(select(m.company_sources)).mappings().one()
            job = connection.execute(select(m.jobs)).mappings().one()
            collector = connection.execute(select(m.job_sources)).mappings().one()
        self.assertEqual(company['id'], 'canonical-employer')
        self.assertEqual(company['name'], 'Example Systems')
        self.assertEqual(job['company_id'], company['id'])
        self.assertEqual(source['company_id'], company['id'])
        self.assertTrue(source['discovered_automatically'])
        self.assertTrue(source['enabled'])
        self.assertEqual(source['collector_source_id'], collector['id'])
        self.assertEqual(source['career_url'], 'https://employer.example/careers')
        self.assertIsNotNone(source['last_success'])
        with patch.dict(os.environ, self.env), TestClient(create_app(self.store, "http://127.0.0.1:18799", self.root / "outputs", redis_url="redis://127.0.0.1:1/0", operation_mode="PERSONAL")) as client:
            response = client.get('/company-sources/view', params={'profile_id': 'fixture'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(source['id'], response.text)
            view = response.json()['company_sources'][0]
            self.assertEqual(view['jobs_found'], 1)
            self.assertEqual(view['collector']['status'], 'ACTIVE')
        reopened = Store(self.url)
        try:
            with reopened.engine.begin() as connection:
                connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
            scheduled = schedule_due_sources(reopened)
            self.assertEqual(len(scheduled['scheduled']), 1, scheduled)
            self.jobs.append(dict(self.jobs[0], id=102, absolute_url='https://job-boards.greenhouse.io/example/jobs/102'))
            with patch.dict(os.environ, self.env), patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None: self.board(adapter, suffix, params)):
                refreshed = execute_operation(scheduled['scheduled'][0]['operation_id'])
            self.assertEqual(refreshed['status'], 'COMPLETED', refreshed)
            self.assertEqual(reopened.count('canonical_jobs'), 2)
            self.assertEqual(reopened.count('companies'), 1)
            self.assertEqual(reopened.count('company_job_sources'), 1)
        finally:
            reopened.engine.dispose()

    def test_disabled_bound_source_is_not_scheduled_or_imported(self):
        from services.company_discovery.sources import set_company_source_enabled
        from services.discovery.sources.collect import collect_source
        self.assertEqual(self.discover()['status'], 'COMPLETED')
        with self.store.engine.begin() as connection:
            source = connection.execute(select(m.company_sources)).mappings().one()
            connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
        set_company_source_enabled(self.store, source['id'], enabled=False)
        scheduled = schedule_due_sources(self.store)
        self.assertEqual(scheduled['scheduled'], [], scheduled)
        with patch.object(Greenhouse, '_get', side_effect=AssertionError('Disabled source must not fetch')):
            with self.assertRaisesRegex(Exception, 'COMPANY_SOURCE_DISABLED'):
                collect_source(self.store, source['collector_source_id'], 'fixture')
        self.assertEqual(self.store.count('canonical_jobs'), 1)

    def test_unverified_domain_and_ambiguous_board_cannot_enroll(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.company_domains.delete())
        result = self.discover()
        self.assertEqual(result['error_category'], 'COMPANY_DOMAIN_REVIEW_REQUIRED', result)
        self.assertEqual(self.store.count('job_sources'), 0)
        self.assertFalse(any('boards-api' in url for url in self.requests))
        record_company_domain(self.store, 'canonical-employer', 'employer.example',
                              'FIRST_PARTY_JOB_LINK', ['restored verified posting'])
        self.markup += '<iframe src="https://job-boards.greenhouse.io/different"></iframe>'
        # A new user intent does not replay the prior blocked admission.
        from services.company_discovery.flywheel import discover_company_source
        with patch.object(PublicCompanyClient, '_request_once', lambda client, url, deadline: self.page(client, url, deadline)):
            client = PublicCompanyClient(self.candidate['company_website'])
            try:
                with self.assertRaisesRegex(Exception, 'COMPANY_ATS_TENANT_REVIEW_REQUIRED'):
                    discover_company_source(self.store, self.candidate, client, profile_id='fixture')
            finally:
                client.close()
        self.assertEqual(self.store.count('job_sources'), 0)

    def test_existing_collector_disablement_and_policy_are_preserved(self):
        from services.discovery.sources.collect import configure_source
        configure_source(self.store, 'example', enabled=False, poll_interval=900, rate_limit=3)
        result = self.discover()
        self.assertEqual(result['error_category'], 'COMPANY_SOURCE_DISABLED', result)
        with self.store.engine.connect() as connection:
            source = connection.execute(select(m.job_sources)).mappings().one()
        self.assertFalse(source['enabled'])
        self.assertEqual(source['poll_interval'], 900)
        self.assertEqual(source['rate_limit'], 3)
        self.assertEqual(self.store.count('company_job_sources'), 0)
        self.assertEqual(self.store.count('canonical_jobs'), 0)

    def test_revocation_during_fetch_prevents_import_and_further_scheduling(self):
        from services.discovery.sources.collect import collect_source
        self.assertEqual(self.discover()['status'], 'COMPLETED')
        with self.store.engine.begin() as connection:
            source = connection.execute(select(m.company_sources)).mappings().one()
            connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
        self.jobs.append(dict(self.jobs[0], id=102))
        def revoke(adapter, suffix, params=None):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_domains).values(verified=False))
            return self.board(adapter, suffix, params)
        with patch.object(Greenhouse, '_get', revoke):
            with self.assertRaisesRegex(Exception, 'COMPANY_DOMAIN_REVIEW_REQUIRED'):
                collect_source(self.store, source['collector_source_id'], 'fixture')
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        scheduled = schedule_due_sources(self.store)
        self.assertEqual(scheduled['scheduled'], [])
        self.assertTrue(any(row['reason'] == 'COMPANY_DOMAIN_REVIEW_REQUIRED' for row in scheduled['skipped']))
        with self.store.engine.connect() as connection:
            collector = connection.execute(select(m.job_sources)).mappings().one()
        self.assertEqual(collector['consecutive_failures'], 0)
        self.assertEqual(collector['health_status'], 'HEALTHY')

    def test_existing_foreign_board_inventory_cannot_be_reparented(self):
        from services.discovery.sources.collect import collect_source, configure_source
        source_id = configure_source(self.store, 'example')
        with patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None: self.board(adapter, suffix, params)):
            collect_source(self.store, source_id, 'fixture')
        with self.store.engine.connect() as connection:
            original = dict(connection.execute(select(m.jobs)).mappings().one())
        result = self.discover()
        self.assertEqual(result['error_category'], 'COMPANY_ATS_BINDING_REVIEW_REQUIRED', result)
        with self.store.engine.connect() as connection:
            unchanged = dict(connection.execute(select(m.jobs)).mappings().one())
        self.assertEqual(original, unchanged)
        self.assertNotEqual(original['company_id'], 'canonical-employer')

    def test_board_cannot_import_another_tenants_application_link(self):
        self.jobs[0]['absolute_url'] = 'https://job-boards.greenhouse.io/foreign/jobs/101'
        result = self.discover()
        self.assertNotEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(self.store.count('canonical_jobs'), 0)

    def test_aggregator_posting_and_first_party_inventory_share_canonical_job(self):
        from packages.persistence.store import identity
        from services.discovery.sources.collect import collect_source, configure_source
        from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
        with self.store.engine.begin() as connection:
            connection.execute(m.company_domains.delete())
            connection.execute(m.companies.delete())
        source_id = configure_source(self.store, 'default', provider='remotive')
        payload = dict(RAW_REMOTIVE_PAYLOAD, jobs=[RAW_REMOTIVE_PAYLOAD['jobs'][0]])
        collect_source(self.store, source_id, 'fixture',
                       transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
        with self.store.engine.begin() as connection:
            original = connection.execute(select(m.jobs)).mappings().one()
            upsert(connection, m.routes, {'id': identity('observed-first-party-route'),
                'job_id': original['id'], 'company_id': original['company_id'],
                'provider': 'greenhouse', 'tenant': 'example', 'requisition': '101',
                'url': self.jobs[0]['absolute_url']})
        record_company_domain(self.store, original['company_id'], 'employer.example',
                              'FIRST_PARTY_JOB_LINK', ['corroborated original employer posting'])
        result = self.discover()
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(self.store.count('companies'), 1)
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        with self.store.engine.connect() as connection:
            postings = list(connection.execute(select(m.postings)).mappings())
        self.assertEqual(len(postings), 2)
        self.assertEqual({row['job_id'] for row in postings}, {original['id']})

    def test_initial_provider_failure_recovers_through_persisted_collector(self):
        from services.discovery.sources.collect import collect_source
        from services.discovery.sources.errors import SourceError
        queued = queue_company_discovery(self.store, self.candidate, 'USER_ADDED_COMPANY',
                                          profile_id='fixture', public=True)
        with patch.dict(os.environ, self.env), \
                patch.object(PublicCompanyClient, '_request_once', lambda client, url, deadline: self.page(client, url, deadline)), \
                patch.object(Greenhouse, '_get', side_effect=SourceError('SOURCE_HTTP_503')):
            result = execute_operation(queued['operation_id'])
        self.assertNotEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(self.store.count('canonical_jobs'), 0)
        reopened = Store(self.url)
        try:
            with reopened.engine.begin() as connection:
                source = connection.execute(select(m.company_sources)).mappings().one()
                self.assertIsNone(source['last_success'])
                connection.execute(update(m.job_sources).values(
                    last_attempt='2000-01-01T00:00:00+00:00', backoff_until=None))
            with patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None: self.board(adapter, suffix, params)):
                collect_source(reopened, source['collector_source_id'], 'fixture')
            self.assertEqual(reopened.count('canonical_jobs'), 1)
            self.assertEqual(reopened.count('company_job_sources'), 1)
            self.assertEqual(reopened.count('company_ats_tenants'), 1)
        finally:
            reopened.engine.dispose()

    def test_explicit_saved_search_source_scope_is_not_broadened(self):
        from services.company_discovery.sources import list_company_source_view
        from services.discovery.sources.collect import configure_source
        other = configure_source(self.store, 'default', provider='remotive', enabled=False)
        save_search_profile(self.store, {'id': 'all-sources', 'profile_id': 'fixture',
                            'name': 'All employers', 'enabled': True, 'source_ids': [other]})
        self.assertEqual(self.discover()['status'], 'COMPLETED')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
            self.assertEqual(connection.execute(select(m.search_profiles.c.source_ids)).scalar_one(), [other])
        self.assertEqual(schedule_due_sources(self.store)['scheduled'], [])
        self.assertEqual(list_company_source_view(self.store, 'fixture')[0]['collector']['status'], 'NO_SCHEDULED_SEARCH')


if __name__ == '__main__':
    unittest.main()
