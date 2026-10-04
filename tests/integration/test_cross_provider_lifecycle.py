"""Real feed ingestion must join exact requisitions before resolving lifecycle."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from apps.api.main import create_app
from services.company_discovery.domains import record_company_domain
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs
from services.documents.generate import prepare
from tests.integration import test_job_lifecycle as lifecycle_fixtures
from tests.integration.test_greenhouse import RAW
from tests.integration import test_source_value_resolution as fixtures


class CrossProviderLifecycleTests(unittest.TestCase):
    setUp = fixtures.SourceValueResolutionTests.setUp
    tearDown = fixtures.SourceValueResolutionTests.tearDown
    source = fixtures.SourceValueResolutionTests.source

    def sources(self):
        record_company_domain(self.store, 'company', 'employer.example',
                              'FIRST_PARTY_JOB_LINK', ['Independent synthetic evidence'])
        return [upsert_company_job_source(self.store, company_id='company', provider=provider,
                    tenant_identifier=tenant, career_url=origin+'/careers', feed_url=origin+'/jobs.xml',
                    collection_strategy='RSS', discovered_automatically=True)
                for provider, tenant, origin in [('rss', 'employer', 'https://employer.example'),
                                                  ('feed', 'aggregator', 'https://indeed.com')]]

    def ingest(self, source, native_id, status, url='https://employer.example/jobs/123'):
        return import_company_feed_jobs(self.store, company_id=source['company_id'], source_id=source['id'],
            provider=source['provider'], tenant=source['tenant'], endpoint=source['endpoint'],
            strategy='RSS', items=[dict(guid=native_id, title='Python Engineer', link=url,
                description='Build Python services.', status=status)], profile_id='fixture')

    def test_employer_closure_wins_across_native_ids_and_tracking_urls(self):
        employer, aggregator = self.sources()
        self.ingest(employer, 'req-123', 'CLOSED')
        job_id = identity('company', 'rss', 'employer', 'req-123')
        before = self.store.job(job_id)
        self.ingest(aggregator, 'listing-999', 'ACTIVE',
                    'https://employer.example/jobs/123?utm_source=board')
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        job = self.store.job(job_id)
        self.assertEqual(job['status'], 'CLOSED')
        self.assertEqual(len(job['postings']), 2)
        self.assertEqual(len(job['lifecycle_history']), 2)
        self.assertEqual(job['description_revisions'][0], before['description_revisions'][0])
        self.assertEqual(self.store.count('application_routes'), 1)
        with self.store.engine.connect() as c:
            candidates = c.execute(select(m.posting_value_candidates.c.normalized)).scalars().all()
        self.assertEqual({v['requisition'] for v in candidates}, {'req-123', 'listing-999'})

    def test_reverse_arrival_and_refresh_keep_identity_after_restart(self):
        employer, aggregator = self.sources()
        self.ingest(aggregator, 'listing-999', 'ACTIVE')
        job_id = identity('company', 'feed', 'aggregator', 'listing-999')
        self.ingest(employer, 'req-123', 'CLOSED')
        self.ingest(aggregator, 'listing-999', 'ACTIVE')
        self.store.engine.dispose()
        self.store = Store(self.store.url)
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        self.assertEqual(self.store.job(job_id)['status'], 'CLOSED')
        self.assertEqual(len(self.store.job(job_id)['postings']), 2)

    def test_landing_pages_and_distinct_requisitions_stay_separate(self):
        employer, aggregator = self.sources()
        for index, url in enumerate(('https://employer.example/', 'https://employer.example/careers',
                                     'https://employer.example/jobs/search')):
            self.ingest(employer, f'employer-{index}', 'CLOSED', url)
            self.ingest(aggregator, f'board-{index}', 'ACTIVE', url)
        self.ingest(employer, 'req-123', 'CLOSED')
        self.ingest(aggregator, 'listing-999', 'ACTIVE', 'https://employer.example/jobs/456')
        self.assertEqual(self.store.count('canonical_jobs'), 8)

    def test_attached_posting_does_not_move_when_application_url_changes(self):
        employer, aggregator = self.sources()
        self.ingest(employer, 'req-123', 'CLOSED')
        self.ingest(aggregator, 'listing-999', 'ACTIVE')
        self.ingest(aggregator, 'listing-999', 'ACTIVE', 'https://employer.example/jobs/456')
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        job = self.store.job(identity('company', 'rss', 'employer', 'req-123'))
        self.assertEqual(job['status'], 'CLOSED')
        self.assertEqual(len(job['postings']), 2)
        self.assertEqual(sum(len(p['snapshots']) for p in job['postings']), 3)

    def test_existing_application_route_and_history_survive_attachment(self):
        employer, aggregator = self.sources()
        self.ingest(employer, 'req-123', 'ACTIVE')
        job_id = identity('company', 'rss', 'employer', 'req-123')
        application_id = prepare(self.store, job_id, 'fixture', self.root/'docs')
        before = self.store.applications()[0]
        self.ingest(aggregator, 'listing-999', 'ACTIVE')
        self.ingest(employer, 'req-123', 'CLOSED')
        self.assertEqual(self.store.applications()[0], before)
        self.assertEqual(before['id'], application_id)
        with TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='FIXTURE')) as client:
            detail = client.get('/jobs/'+job_id).json()
            self.assertEqual(detail['status'], 'CLOSED')
            self.assertEqual(len(detail['postings']), 2)
        self.assertEqual(self.store.count('application_routes'), 1)

    def test_concurrent_sources_choose_one_canonical_identity(self):
        employer, aggregator = self.sources()
        barrier = Barrier(2)
        def ingest(source, native_id, status):
            barrier.wait(timeout=10)
            return self.ingest(source, native_id, status)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(ingest, employer, 'req-123', 'CLOSED'),
                       pool.submit(ingest, aggregator, 'listing-999', 'ACTIVE')]
            self.assertEqual([future.result(timeout=30) for future in futures], [1, 1])
        self.assertEqual(self.store.count('canonical_jobs'), 1)
        self.assertEqual(self.store.jobs()[0]['status'], 'CLOSED')
        self.assertEqual(self.store.count('job_postings'), 2)

    def test_existing_duplicate_jobs_are_not_reparented_on_refresh(self):
        employer, aggregator = self.sources()
        self.ingest(employer, 'req-123', 'CLOSED')
        self.ingest(aggregator, 'listing-999', 'ACTIVE', 'https://employer.example/jobs/456')
        self.ingest(aggregator, 'listing-999', 'ACTIVE')
        self.assertEqual(self.store.count('canonical_jobs'), 2)
        self.assertEqual(self.store.job(identity('company', 'feed', 'aggregator', 'listing-999'))['status'], 'ACTIVE')

    def test_same_url_at_another_company_is_not_an_identity_match(self):
        employer, _ = self.sources()
        self.ingest(employer, 'req-123', 'CLOSED')
        with self.store.engine.begin() as c:
            upsert(c, m.companies, {'id': 'another', 'name': 'Synthetic Employer', 'domain': ''})
        other = upsert_company_job_source(self.store, company_id='another', provider='rss',
            tenant_identifier='another', feed_url='https://indeed.com/jobs.xml', collection_strategy='RSS')
        self.ingest(other, 'listing-999', 'ACTIVE')
        self.assertEqual(self.store.count('canonical_jobs'), 2)

    def test_native_adapter_attaches_and_inventory_absence_keeps_employer_closure(self):
        company_id = identity('greenhouse', 'example')
        with self.store.engine.begin() as c:
            upsert(c, m.companies, {'id': company_id, 'name': 'Synthetic Employer', 'domain': ''})
        record_company_domain(self.store, company_id, 'employer.example',
                              'FIRST_PARTY_JOB_LINK', ['Independent synthetic evidence'])
        employer = upsert_company_job_source(self.store, company_id=company_id, provider='rss',
            tenant_identifier='employer', feed_url='https://employer.example/jobs.xml', collection_strategy='RSS')
        self.ingest(employer, 'req-123', 'CLOSED', RAW['absolute_url'])
        job_id = identity(company_id, 'rss', 'employer', 'req-123')
        original_history = self.store.job(job_id)['lifecycle_history']
        lifecycle_fixtures.CompleteInventoryTests.collect(self, [RAW, dict(RAW, id=1235)])
        # Native requisitions 1234 and 1235 remain distinct even when the first
        # one attaches to a job initially established through another provider.
        self.assertEqual(self.store.count('canonical_jobs'), 2)
        self.assertEqual(len(self.store.job(job_id)['postings']), 2)
        self.assertEqual(self.store.job(job_id)['status'], 'CLOSED')
        lifecycle_fixtures.CompleteInventoryTests.collect(self, [])
        job = self.store.job(job_id)
        self.assertEqual(job['status'], 'CLOSED')
        self.assertEqual(job['lifecycle_history'][0], original_history[0])
        self.assertIn('REMOVED', {row['status'] for row in job['lifecycle_history']})


if __name__ == '__main__':
    unittest.main()
