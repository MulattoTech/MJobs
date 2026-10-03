"""Observed source authority/quality must decide actual canonical job values."""
import tempfile
from pathlib import Path
import unittest

from sqlalchemy import select
from fastapi.testclient import TestClient

from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.company_discovery.domains import record_company_domain
from services.discovery.company_feed import import_company_feed_jobs
from services.documents.generate import prepare, PreparationError
from apps.api.main import create_app
from tests.integration.test_spine import PROFILE


class SourceValueResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'values.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(PROFILE)
        with self.store.engine.begin() as c:
            upsert(c, m.companies, {'id': 'company', 'name': 'Synthetic Employer', 'domain': ''})
        self.rich = self.source('https://rich.example')
        self.sparse = self.source('https://sparse.example')
        self.job_id = identity('company', 'rss', 'company', 'same-role')

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def source(self, origin):
        return upsert_company_job_source(self.store, company_id='company', provider='rss',
            tenant_identifier='company', career_url=origin+'/careers', feed_url=origin+'/jobs.xml',
            collection_strategy='RSS', discovered_automatically=True)

    def collect(self, source, rich, *, title=None, link='/jobs/one'):
        row = {'guid': 'same-role', 'title': title or ('Python Engineer' if rich else 'Sparse role'), 'link': link}
        if rich:
            row.update(description='Python engineering. Salary $90,000 - $110,000 per year.', location='Remote - United States')
        return import_company_feed_jobs(self.store, company_id='company', source_id=source['id'],
            provider='rss', tenant='company', endpoint=source['endpoint'], strategy='RSS',
            items=[row], profile_id='fixture', fetch_available=True)

    def test_low_quality_later_import_does_not_replace_rich_job_or_its_match(self):
        self.collect(self.rich, True)
        expected = self.store.job(self.job_id, 'fixture')
        self.collect(self.sparse, False)
        actual = self.store.job(self.job_id, 'fixture')
        for field in ('title','description','skills','location','remote_type','salary_min','salary_max','salary_period'):
            self.assertEqual(actual[field], expected[field], field)
        self.assertEqual(actual['match']['score'], expected['match']['score'])
        self.assertEqual(actual['application_url'], expected['application_url'])
        resolution = actual['attributes']['source_resolution']
        self.assertEqual(resolution['candidate_count'], 2)
        self.assertIn('title', resolution['conflicts'])
        self.assertEqual(resolution['authority'], 'UNKNOWN')
        self.assertEqual(len(actual['postings']), 2)

    def test_reverse_import_order_selects_same_source(self):
        self.collect(self.sparse, False)
        self.collect(self.rich, True)
        job = self.store.job(self.job_id, 'fixture')
        self.assertEqual(job['title'], 'Python Engineer')
        self.assertEqual(job['attributes']['source_resolution']['candidate_count'], 2)

    def test_verified_employer_authority_beats_better_quality_unverified_source(self):
        record_company_domain(self.store, 'company', 'sparse.example', 'FIRST_PARTY_JOB_LINK',
                              ['Synthetic independent first-party evidence'])
        self.collect(self.sparse, False)
        self.collect(self.rich, True)
        job = self.store.job(self.job_id, 'fixture')
        self.assertEqual(job['title'], 'Sparse role')
        self.assertEqual(job['description'], '')
        self.assertIsNone(job['salary_min'])
        self.assertEqual(job['attributes']['source_resolution']['authority'], 'VERIFIED_EMPLOYER')

    def test_repeated_source_refresh_keeps_old_normalized_and_raw_evidence(self):
        self.collect(self.rich, True)
        self.collect(self.rich, True, title='Updated Python role')
        job = self.store.job(self.job_id)
        self.assertEqual(job['title'], 'Updated Python role')
        self.assertEqual(len(job['postings']), 1)
        self.assertEqual(len(job['postings'][0]['snapshots']), 2)
        with self.store.engine.connect() as c:
            rows = list(c.execute(select(m.posting_value_candidates)).mappings())
        self.assertEqual({row['normalized']['title'] for row in rows}, {'Python Engineer','Updated Python role'})

    def test_prepared_route_is_frozen_and_explicit_review_required_at_api_and_worker(self):
        self.collect(self.rich, True)
        application_id = prepare(self.store, self.job_id, 'fixture', self.root/'docs')
        old_url = self.store.job(self.job_id)['application_url']
        self.collect(self.rich, True, link='/jobs/revised')
        current = self.store.job(self.job_id)
        resolution = current['attributes']['source_resolution']
        self.assertTrue(resolution['route_review_required'])
        self.assertEqual(resolution['preserved_route_url'], old_url)
        self.assertNotEqual(current['application_url'], old_url)
        self.assertEqual(self.store.applications()[0]['application_url'], old_url)
        with self.assertRaises(PreparationError) as raised:
            prepare(self.store, self.job_id, 'fixture', self.root/'docs')
        self.assertEqual(raised.exception.code, 'APPLICATION_SOURCE_ROUTE_REVIEW_REQUIRED')
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            response = client.post('/applications', json={'job_id': self.job_id,'profile_id':'fixture','request_id':'route-review'})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.store.count('worker_operations'), 0)
        self.assertEqual(prepare(self.store, self.job_id, 'fixture', self.root/'docs',
            route_mode='THIS_ROUTE', route_id=resolution['preserved_route_id']), application_id)
        self.assertEqual(self.store.applications()[0]['application_url'], old_url)

    def test_explicit_route_choice_never_substitutes_a_preferred_alias(self):
        from services.dedupe.routes import plan_application_routes
        rows = [dict(id='requested', company_id='company',provider='unknown',tenant='company',
                     requisition='req',url='https://first.example/req'),
                dict(id='preferred',company_id='company',provider='greenhouse',tenant='company',
                     requisition='req',url='https://job-boards.greenhouse.io/company/jobs/1')]
        plan = plan_application_routes(rows, 'THIS_ROUTE', 'requested')
        self.assertEqual(plan['selected'][0]['preferred_route']['id'], 'requested')
