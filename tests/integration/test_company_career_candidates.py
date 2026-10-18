"""Official career candidates through PERSONAL admission, worker and readback.

Only outbound employer/ATS responses are synthetic. Identity, request admission,
operation execution, enrollment, collection, matching and persistence are real.
"""
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
from packages.persistence.store import Store, identity, now, upsert
from services.company_discovery.domains import record_company_domain
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.greenhouse import Greenhouse
from services.discovery.sources.errors import SourceError
from services.discovery.sources.collect import collect_source, configure_source
from services.dedupe.source_values import resolve_job_values
from services.dedupe.routes import plan_application_routes
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


ORIGIN = 'https://employer.example'
BOARD = '<a href="https://job-boards.greenhouse.io/example">Open jobs</a>'
JOB_FEED = ('<rss><channel><item><guid>employer-role</guid><title>Python Engineer</title>'
            '<link>https://employer.example/jobs/1</link>'
            '<description>Python SQL remote role</description></item></channel></rss>')
BLOG_FEED = ('<rss><channel><item><guid>news</guid><title>Company picnic</title>'
             '<link>https://employer.example/news/picnic</link></item></channel></rss>')


class CompanyCareerCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'careers.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(PROFILE)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {'id': 'employer', 'name': 'Example Systems', 'domain': ''})
        record_company_domain(self.store, 'employer', 'employer.example',
                              'FIRST_PARTY_JOB_LINK', ['synthetic independently verified employer posting'])
        save_search_profile(self.store, {'id': 'all-sources', 'profile_id': 'fixture',
                            'name': 'All employers', 'enabled': True, 'source_ids': []})
        self.env = {'MJOBS_DATABASE_URL': self.store.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
                    'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0',
                    'MJOBS_OUTPUT_DIR': str(self.root / 'outputs')}
        self.pages = {'/robots.txt': 'User-agent: *\nAllow: /',
                      '/': '<a href="/culture">Careers</a><a href="/team/openings">Open roles</a>',
                      '/culture': '<h1>Life at Example Systems</h1>', '/team/openings': BOARD}
        self.seen = []
        self.board_failure = None
        self.jobs = [{'id': 101, 'title': 'Python Engineer', 'content': '<p>Python SQL remote role</p>',
                      'location': {'name': 'Remote'},
                      'absolute_url': 'https://job-boards.greenhouse.io/example/jobs/101'}]

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def page(self, client, url, deadline):
        self.seen.append(url)
        request = httpx.Request('GET', url)
        path = request.url.path
        return httpx.Response(200 if path in self.pages else 404,
                              text=self.pages.get(path, ''), request=request)

    def board(self, adapter, suffix, params=None):
        if self.board_failure:
            raise self.board_failure
        return {'jobs': self.jobs} if suffix else {'name': 'Provider display name'}

    def app(self, store=None):
        return create_app(store or self.store, 'http://127.0.0.1:18799', self.root / 'outputs',
                          redis_url='redis://127.0.0.1:1/0', operation_mode='PERSONAL')

    def run_discovery(self, website=ORIGIN + '/'):
        with patch.dict(os.environ, self.env), TestClient(self.app()) as client:
            response = client.post('/companies/discovery', json={
                'company_name': 'Untrusted display name', 'company_website': website,
                'reason': 'USER_ADDED_COMPANY', 'profile_id': 'fixture'})
            self.assertEqual(response.status_code, 202, response.text)
            operation_id = response.json()['operation_id']
            with patch.object(PublicCompanyClient, '_request_once',
                              lambda transport, url, deadline: self.page(transport, url, deadline)), \
                    patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None:
                                 self.board(adapter, suffix, params)):
                result = execute_operation(operation_id)
        return operation_id, result

    def assert_enrolled(self, career_url):
        # A newly opened Store and API prove durable user-visible readback.
        reopened = Store(self.store.url)
        try:
            with patch.dict(os.environ, self.env), TestClient(self.app(reopened)) as client:
                response = client.get('/company-sources/view', params={'profile_id': 'fixture'})
                self.assertEqual(response.status_code, 200, response.text)
                sources = response.json()['company_sources']
                self.assertEqual(len(sources), 1)
                self.assertEqual(sources[0]['jobs_found'], 1)
            with reopened.engine.connect() as connection:
                source = connection.execute(select(m.company_sources)).mappings().one()
                job = connection.execute(select(m.jobs)).mappings().one()
            self.assertEqual(source['career_url'], career_url)
            self.assertEqual(source['company_id'], 'employer')
            self.assertEqual(source['provider'], 'greenhouse')
            self.assertEqual(source['tenant'], 'example')
            self.assertEqual(job['company_id'], 'employer')
            detail = reopened.job(job['id'], profile_id='fixture')
            self.assertEqual(detail['application_url'], self.jobs[0]['absolute_url'])
            self.assertEqual(reopened.count('application_attempts'), 0)
        finally:
            reopened.engine.dispose()

    def test_later_official_career_link_enrolls_when_first_link_is_a_culture_page(self):
        _, result = self.run_discovery()
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assert_enrolled(ORIGIN + '/team/openings')

    def test_supplied_career_landing_page_uses_its_existing_ats_evidence(self):
        _, result = self.run_discovery(ORIGIN + '/team/openings')
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assert_enrolled(ORIGIN + '/team/openings')
        self.assertNotIn(ORIGIN + '/sitemap.xml', self.seen)

    def test_generic_homepage_rss_does_not_hide_career_ats_or_import_news(self):
        self.pages['/'] = ('<link rel="alternate" type="application/rss+xml" href="/feed/">'
                           '<a href="/careers">Careers</a>')
        self.pages['/feed/'] = BLOG_FEED
        self.pages['/careers'] = BOARD
        _, result = self.run_discovery()
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assert_enrolled(ORIGIN + '/careers')
        self.assertNotIn(ORIGIN + '/feed/', self.seen)
        self.assertEqual(self.store.count('company_ats_tenants'), 1)

    def test_generic_homepage_feed_alone_is_not_evidence_of_job_inventory(self):
        self.pages['/'] = '<link rel="alternate" type="application/rss+xml" href="/feed/">'
        self.pages['/feed/'] = BLOG_FEED
        _, result = self.run_discovery()
        self.assertNotEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(self.store.count('canonical_jobs'), 0)
        self.assertEqual(self.store.count('company_job_sources'), 0)
        self.assertNotIn(ORIGIN + '/feed/', self.seen)

    def test_stale_career_redirect_to_generic_homepage_feed_does_not_hide_later_ats(self):
        self.pages['/'] += '<link rel="alternate" type="application/rss+xml" href="/feed/">'
        self.pages['/feed/'] = BLOG_FEED
        original_page = self.page

        def redirect(transport, url, deadline):
            if url == ORIGIN + '/culture':
                return httpx.Response(302, headers={'location': '/'}, request=httpx.Request('GET', url))
            return original_page(transport, url, deadline)

        with patch.object(self, 'page', side_effect=redirect):
            _, result = self.run_discovery()
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assert_enrolled(ORIGIN + '/team/openings')
        self.assertNotIn(ORIGIN + '/feed/', self.seen)

    def assert_job_feed(self, website, endpoint):
        _, result = self.run_discovery(website)
        self.assertEqual(result['status'], 'COMPLETED', result)
        with self.store.engine.connect() as connection:
            source = connection.execute(select(m.company_sources)).mappings().one()
            job = connection.execute(select(m.jobs)).mappings().one()
        self.assertEqual(source['endpoint'], endpoint.rstrip('/'))
        self.assertEqual(source['provider'], 'rss')
        self.assertEqual(job['title'], 'Python Engineer')
        self.assertEqual(self.store.count('application_attempts'), 0)

    def test_homepage_job_feed_wins_over_unrelated_preferred_format_news_feed(self):
        self.pages['/'] = ('<link rel="alternate" type="application/feed+json" href="/news.json">'
                           '<link rel="alternate" type="application/rss+xml" href="/jobs.xml">')
        self.pages['/news.json'] = ('{"version":"https://jsonfeed.org/version/1.1",'
                                  '"items":[{"id":"news","title":"Company picnic",'
                                  '"url":"https://employer.example/news/picnic"}]}')
        self.pages['/jobs.xml'] = JOB_FEED
        self.assert_job_feed(ORIGIN + '/', ORIGIN + '/jobs.xml')
        self.assertNotIn(ORIGIN + '/news.json', self.seen)

    def test_supplied_career_page_preserves_its_generic_named_job_feed(self):
        self.pages['/careers'] = '<link rel="alternate" type="application/rss+xml" href="/feed/">'
        self.pages['/feed/'] = JOB_FEED
        self.assert_job_feed(ORIGIN + '/careers', ORIGIN + '/feed/')

    def test_homepage_explicit_job_feed_label_preserves_generic_feed_url(self):
        self.pages['/'] = ('<link rel="alternate" type="application/rss+xml" '
                           'title="Current job openings" href="/feed/">')
        self.pages['/feed/'] = JOB_FEED
        self.assert_job_feed(ORIGIN + '/', ORIGIN + '/feed/')

    def test_career_homepage_heading_preserves_generic_feed_url(self):
        self.pages['/'] = ('<h1>Careers at Example Systems</h1>'
                           '<link rel="alternate" type="application/rss+xml" href="/feed/">')
        self.pages['/feed/'] = JOB_FEED
        self.assert_job_feed(ORIGIN + '/', ORIGIN + '/feed/')

    def test_missing_first_link_does_not_hide_later_official_openings(self):
        del self.pages['/culture']
        _, result = self.run_discovery()
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assert_enrolled(ORIGIN + '/team/openings')

    def test_candidate_limit_preserves_review_without_crawling_the_site(self):
        self.pages['/'] = ''.join(f'<a href="/team/{i}">Open roles</a>' for i in range(6))
        self.pages.update({f'/team/{i}': '<h1>Life at Example Systems</h1>' for i in range(5)})
        self.pages['/team/5'] = BOARD
        _, result = self.run_discovery()
        self.assertEqual(result['error_category'], 'COMPANY_ATS_REVIEW_REQUIRED', result)
        self.assertNotIn(ORIGIN + '/team/5', self.seen)
        self.assertEqual(self.store.count('company_job_sources'), 0)
        self.assertEqual(self.store.count('canonical_jobs'), 0)
        self.assertLessEqual(len(self.seen), 17)

    def test_off_origin_and_private_links_cannot_supply_an_employer_board(self):
        self.pages['/'] = ('<a href="http://127.0.0.1/careers">Careers</a>'
                           '<a href="https://foreign.example/careers">Jobs</a>'
                           '<a href="/team/openings">Open roles</a>')
        _, result = self.run_discovery()
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertTrue(all(url.startswith(ORIGIN + '/') for url in self.seen))
        self.assert_enrolled(ORIGIN + '/team/openings')

    def test_robots_denial_stops_candidate_traversal(self):
        self.pages['/robots.txt'] = 'User-agent: *\nDisallow: /culture'
        _, result = self.run_discovery()
        self.assertNotEqual(result['status'], 'COMPLETED', result)
        self.assertNotIn(ORIGIN + '/culture', self.seen)
        self.assertNotIn(ORIGIN + '/team/openings', self.seen)
        self.assertEqual(self.store.count('company_job_sources'), 0)

    def test_later_board_evidence_does_not_verify_an_unproven_employer_domain(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.company_domains.delete())
        _, result = self.run_discovery()
        self.assertEqual(result['error_category'], 'COMPANY_DOMAIN_REVIEW_REQUIRED', result)
        self.assertEqual(self.store.count('company_job_sources'), 0)
        self.assertEqual(self.store.count('canonical_jobs'), 0)

    def test_completed_worker_replay_does_not_refetch_or_duplicate_inventory(self):
        operation_id, result = self.run_discovery()
        self.assertEqual(result['status'], 'COMPLETED', result)
        with patch.dict(os.environ, self.env), \
                patch.object(PublicCompanyClient, '_request_once', side_effect=AssertionError('Replay egress')), \
                patch.object(Greenhouse, '_get', side_effect=AssertionError('Replay ATS read')):
            replay = execute_operation(operation_id)
        self.assertEqual(replay['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assert_enrolled(ORIGIN + '/team/openings')
        self.assertEqual(self.store.count('job_postings'), 1)

    def test_provider_failure_after_later_link_enrollment_recovers_from_persisted_source(self):
        self.board_failure = SourceError('SOURCE_HTTP_503')
        _, result = self.run_discovery()
        self.assertNotEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(self.store.count('canonical_jobs'), 0)
        reopened = Store(self.store.url)
        try:
            with reopened.engine.begin() as connection:
                source = connection.execute(select(m.company_sources)).mappings().one()
                self.assertEqual(source['career_url'], ORIGIN + '/team/openings')
                self.assertIsNone(source['last_success'])
                connection.execute(update(m.job_sources).values(
                    last_attempt='2000-01-01T00:00:00+00:00', backoff_until=None))
            self.board_failure = None
            with patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None:
                              self.board(adapter, suffix, params)):
                collect_source(reopened, source['collector_source_id'], 'fixture')
            self.assertEqual(reopened.count('canonical_jobs'), 1)
            self.assertEqual(reopened.count('company_job_sources'), 1)
            self.assertEqual(reopened.count('company_ats_tenants'), 1)
        finally:
            reopened.engine.dispose()
        self.assert_enrolled(ORIGIN + '/team/openings')

    def produce_aggregator(self, **raw_claims):
        company_id = identity('remoteok', 'example-systems')
        with self.store.engine.begin() as connection:
            connection.execute(m.company_domains.delete())
            connection.execute(m.companies.delete())
            upsert(connection, m.companies, {'id': company_id, 'name': 'Example Systems', 'domain': ''})
        record_company_domain(self.store, company_id, 'employer.example',
                              'FIRST_PARTY_JOB_LINK', ['synthetic independently verified employer posting'])
        aggregator = configure_source(self.store, 'default', provider='remoteok')
        payload = [{'id': '9001', 'company': 'Example Systems', 'position': 'Python Engineer',
                    'description': 'Python SQL remote role', 'location': 'Remote',
                    'apply_url': self.jobs[0]['absolute_url'], **raw_claims}]
        collect_source(self.store, aggregator, 'fixture',
                       transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
        with self.store.engine.connect() as connection:
            original_id = connection.execute(select(m.jobs.c.id)).scalar_one()
        return company_id, original_id

    def execute_automatic_discovery(self):
        with patch.dict(os.environ, self.env), TestClient(self.app()) as client:
            requests = client.get('/company-discovery/requests').json()['requests']
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['status'], 'QUEUED')
            self.assertEqual(requests[0]['authorization'], 'VERIFIED_DOMAIN')
            operation_id = requests[0]['operation_id']
            with patch.object(PublicCompanyClient, '_request_once',
                              lambda transport, url, deadline: self.page(transport, url, deadline)), \
                    patch.object(Greenhouse, '_get', lambda adapter, suffix, params=None:
                                 self.board(adapter, suffix, params)):
                result = execute_operation(operation_id)
            self.assertEqual(result['status'], 'COMPLETED', result)

    def test_aggregator_producer_finds_employer_only_job_and_preserves_deduped_route(self):
        company_id, original_id = self.produce_aggregator()
        self.jobs.append({'id': 102, 'title': 'Data Engineer', 'content': '<p>Python SQL remote role</p>',
                          'location': {'name': 'Remote'},
                          'absolute_url': 'https://job-boards.greenhouse.io/example/jobs/102'})
        self.execute_automatic_discovery()
        reopened = Store(self.store.url)
        try:
            self.assertEqual(reopened.count('canonical_jobs'), 2)
            self.assertEqual(reopened.count('companies'), 1)
            self.assertEqual(reopened.count('job_postings'), 3)
            with patch.dict(os.environ, self.env), TestClient(self.app(reopened)) as client:
                response = client.get(f'/jobs/{original_id}', params={'profile_id': 'fixture'})
                self.assertEqual(response.status_code, 200, response.text)
                detail = response.json()
            self.assertEqual(detail['company_id'], company_id)
            self.assertEqual(detail['provider'], 'greenhouse')
            self.assertEqual(detail['application_url'], self.jobs[0]['absolute_url'])
            self.assertEqual(detail['attributes']['source_resolution']['authority'], 'VERIFIED_ATS')
            self.assertEqual(len(detail['postings']), 2)
            self.assertEqual({row['provider'] for row in detail['routes']}, {'greenhouse', 'remoteok'})
            plan = plan_application_routes(detail['routes'])
            route = plan['selected'][0]['preferred_route']
            self.assertEqual(route['provider'], 'greenhouse')
            self.assertEqual(route['authority'], 'VERIFIED_ATS')
            self.assertEqual(route['url'], self.jobs[0]['absolute_url'])
            with reopened.engine.connect() as connection:
                employer_only = connection.execute(select(m.jobs).where(m.jobs.c.id != original_id)).mappings().one()
            employer_detail = reopened.job(employer_only['id'], profile_id='fixture')
            self.assertEqual(employer_detail['title'], 'Data Engineer')
            self.assertEqual(employer_detail['application_url'], self.jobs[1]['absolute_url'])
            self.assertEqual(len(employer_detail['postings']), 1)
            self.assertEqual(employer_detail['match']['score'], 100)
            self.assertEqual(reopened.count('application_attempts'), 0)
        finally:
            reopened.engine.dispose()

    def assert_bound_source_loses_authority(self, table, values):
        _, original_id = self.produce_aggregator()
        self.execute_automatic_discovery()
        before = self.store.job(original_id, profile_id='fixture')
        self.assertEqual(before['attributes']['source_resolution']['authority'], 'VERIFIED_ATS')
        with self.store.engine.begin() as connection:
            query = update(table)
            if table is m.company_sources:
                query = query.where(table.c.provider == 'greenhouse')
            connection.execute(query.values(**values))
            resolved = resolve_job_values(connection, original_id, at=now())
        self.assertEqual(resolved['attributes']['provider'], 'remoteok')
        candidates = resolved['attributes']['source_resolution']['candidates']
        self.assertEqual({item['authority'] for item in candidates}, {'AGGREGATOR', 'UNKNOWN'})
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        self.assertEqual(self.store.count('job_postings'), 2)
        self.assertEqual(len(self.store.job(original_id)['routes']), 2)

    def test_revoked_domain_cannot_authorize_the_historical_ats_candidate(self):
        self.assert_bound_source_loses_authority(m.company_domains, {'verified': False})

    def test_retargeted_endpoint_cannot_lend_authority_to_the_old_ats_candidate(self):
        self.assert_bound_source_loses_authority(m.company_sources,
            {'endpoint': 'https://boards-api.greenhouse.io/v1/boards/foreign/jobs'})

    def test_changed_source_tenant_cannot_authorize_the_historical_ats_candidate(self):
        self.assert_bound_source_loses_authority(m.company_sources, {'tenant': 'foreign'})

    def test_changed_ats_tenant_evidence_cannot_authorize_an_unrelated_board(self):
        self.assert_bound_source_loses_authority(m.company_ats_tenants, {'tenant_identifier': 'foreign'})

    def test_changed_ats_provider_evidence_cannot_authorize_a_greenhouse_candidate(self):
        self.assert_bound_source_loses_authority(m.company_ats_tenants, {'provider': 'lever'})

    def test_removed_collector_binding_cannot_authorize_the_historical_source(self):
        self.assert_bound_source_loses_authority(m.company_sources, {'collector_source_id': None})

    def test_mismatched_tenant_company_cannot_authorize_the_historical_candidate(self):
        _, original_id = self.produce_aggregator()
        self.execute_automatic_discovery()
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {'id': 'foreign', 'name': 'Foreign Employer', 'domain': ''})
            connection.execute(update(m.company_ats_tenants).values(company_id='foreign'))
            resolved = resolve_job_values(connection, original_id, at=now())
        self.assertEqual(resolved['attributes']['provider'], 'remoteok')
        self.assertEqual(resolved['attributes']['source_resolution']['authority'], 'AGGREGATOR')

    def test_disabled_collector_cannot_supply_current_bound_source_authority(self):
        self.assert_bound_source_loses_authority(m.job_sources, {'enabled': False})

    def test_raw_provider_labels_cannot_promote_an_aggregator_to_a_verified_ats(self):
        _, original_id = self.produce_aggregator(provider='greenhouse', tenant='example',
            authority='VERIFIED_ATS', company_website=ORIGIN)
        detail = self.store.job(original_id, profile_id='fixture')
        self.assertEqual(detail['provider'], 'remoteok')
        self.assertEqual(detail['attributes']['source_resolution']['authority'], 'AGGREGATOR')
        self.assertEqual(self.store.count('company_ats_tenants'), 0)


if __name__ == '__main__':
    unittest.main()
