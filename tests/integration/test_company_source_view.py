"""Source-specific confidence, provenance, and explicit operator controls."""
import copy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery import sources
from services.company_discovery.domains import record_company_domain
from tests.integration.test_spine import PROFILE


class CompanySourceViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(copy.deepcopy(PROFILE))
        with self.store.engine.begin() as c:
            upsert(c, m.companies, {'id':'acme', 'name':'Acme', 'domain':'unverified.example'})
        self.app = create_app(self.store, 'http://127.0.0.1:8090', self.root, operation_mode='FIXTURE')

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def source(self, portal='engineering', **changes):
        return sources.upsert_company_job_source(self.store, **{
            'company_id':'acme', 'provider':'rss', 'tenant_identifier':'acme',
            'career_url':f'https://careers.example/{portal}',
            'feed_url':f'https://careers.example/{portal}/jobs.xml',
            'collection_strategy':'RSS', 'discovered_automatically':True,
            'confidence':0.88, **changes})

    def view(self, profile_id=None):
        fn = getattr(sources, 'list_company_source_view', None)
        self.assertTrue(callable(fn), 'Source projection must exist')
        return fn(self.store, profile_id)

    def job(self, source, job_id, posting_id, *, company='acme', score=None, version=None):
        with self.store.engine.begin() as c:
            upsert(c, m.jobs, {'id':job_id, 'company_id':company, 'requisition':job_id,
                'title':'Engineer', 'description':'Python', 'skills':['Python'],
                'location':'Unknown', 'remote_type':'UNKNOWN', 'attributes':{}})
            upsert(c, m.postings, {'id':posting_id, 'job_id':job_id,
                'source':'company-feed:'+source['id'], 'source_job_id':posting_id,
                'source_url':source['feed_url'], 'application_url':'https://careers.example/jobs/'+job_id,
                'raw':{'id':posting_id}, 'fetched_at':'2026-10-02T00:00:00Z'})
            if score is not None:
                upsert(c, m.matches, {'id':job_id+'-'+str(version), 'job_id':job_id,
                    'profile_id':'fixture', 'profile_version':version, 'score':score,
                    'missing':[], 'components':{}})

    def test_official_domain_requires_verified_evidence_and_confidence_is_not_health(self):
        low = self.source(confidence=0.749)
        self.source('federal', confidence=0.75)
        record_company_domain(self.store, 'acme', 'candidate.example')
        rows = {r['id']:r for r in self.view()}
        self.assertIsNone(rows[low['id']]['official_domain'])
        self.assertEqual(rows[low['id']]['status'], 'COMPANY_SOURCE_REVIEW_REQUIRED')
        self.assertEqual({r['status'] for r in rows.values()}, {'COMPANY_SOURCE_REVIEW_REQUIRED','ENABLED'})
        self.assertIsNone(rows[low['id']]['next_check'])
        record_company_domain(self.store, 'acme', 'official.example', 'VERIFIED_EXISTING_DOMAIN', ['verified record'])
        row = self.view()[0]
        self.assertEqual(row['official_domain'], 'official.example')
        self.assertEqual(row['company_name'], 'Acme')
        self.assertNotIn('raw', row)

    def test_counts_are_distinct_portal_scoped_and_current_profile_only(self):
        a, b = self.source(), self.source('federal')
        version = self.store.profile('fixture')['version']
        self.job(a, 'a1', 'a1', score=100, version=version)
        self.job(a, 'a1', 'a1-copy')
        self.job(a, 'a2', 'a2', score=100, version='old-version')
        self.job(b, 'b1', 'b1', score=100, version=version)
        rows = {r['id']:r for r in self.view('fixture')}
        self.assertEqual((rows[a['id']]['jobs_found'], rows[a['id']]['relevant_jobs']), (2,1))
        self.assertEqual((rows[b['id']]['jobs_found'], rows[b['id']]['relevant_jobs']), (1,1))
        self.assertEqual({r['relevant_jobs'] for r in self.view()}, {0})

    def test_provenance_requires_explicit_source_id_and_bounds_payload(self):
        a, b = self.source(), self.source('federal')
        with self.store.engine.begin() as c:
            for n in range(25):
                self.store._audit(c, 'acme', 'COMPANY_SOURCE_DISCOVERED', {
                    'company_source_id':a['id'], 'provider':'rss', 'jobs_imported':n,
                    'endpoint':'https://careers.example/jobs?token=PRIVATE#PRIVATE',
                    'secret':'PRIVATE', 'raw':{'resume':'PRIVATE'}})
            self.store._audit(c, 'acme', 'COMPANY_SOURCE_DISCOVERED', {'provider':'legacy'})
            self.store._audit(c, 'acme', 'COMPANY_SOURCE_DISCOVERED', {'company_source_id':b['id'], 'provider':'other'})
        rows = {r['id']:r for r in self.view()}
        events = rows[a['id']]['provenance']['events']
        self.assertEqual(len(events), 20)
        self.assertEqual({e['details']['provider'] for e in events}, {'rss'})
        self.assertNotIn('PRIVATE', str(events))
        self.assertEqual(len(rows[b['id']]['provenance']['events']), 1)

    def test_pin_and_disable_survive_refresh_and_repeated_requests_are_idempotent(self):
        source = self.source()
        with TestClient(self.app) as client:
            for action, payload in [('pin',{'pinned':True}), ('enabled',{'enabled':False})]:
                for _ in range(2):
                    response=client.put(f"/company-sources/{source['id']}/{action}", json=payload)
                    self.assertEqual(response.status_code, 200, response.text)
            refreshed = self.source(priority='LOW', enabled=True)
            self.assertEqual(refreshed['priority'], 'USER_PINNED')
            self.assertFalse(refreshed['enabled'])
            self.assertEqual(refreshed['origin'], 'AUTO_DISCOVERED')
            client.put(f"/company-sources/{source['id']}/watch", json={'watched':True}).raise_for_status()
            result=client.put(f"/company-sources/{source['id']}/pin", json={'pinned':False})
            self.assertEqual(result.json()['priority'], 'NORMAL')
            self.assertEqual(result.json()['origin'], 'USER_WATCHED')
        with self.store.engine.connect() as c:
            events=list(c.execute(select(m.audit.c.action).where(m.audit.c.entity_id==source['id'])).scalars())
        self.assertEqual(events.count('COMPANY_SOURCE_PIN_CHANGED'), 2)
        self.assertEqual(events.count('COMPANY_SOURCE_ENABLED_CHANGED'), 1)

    def test_view_api_validates_profile_and_protects_cross_site_read(self):
        self.source()
        with TestClient(self.app) as client:
            response=client.get('/company-sources/view')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['company_sources'][0]['relevant_jobs'], 0)
            self.assertEqual(client.get('/company-sources/view?profile_id=missing').status_code, 404)
            self.assertEqual(client.get('/company-sources/view', headers={'sec-fetch-site':'cross-site'}).status_code, 403)

    def test_controls_require_strict_boolean_and_existing_source(self):
        source=self.source()
        with TestClient(self.app) as client:
            for action, field in [('pin','pinned'), ('enabled','enabled')]:
                for bad in ['true', 1, None]:
                    self.assertEqual(client.put(f"/company-sources/{source['id']}/{action}", json={field:bad}).status_code, 422)
                self.assertEqual(client.put(f'/company-sources/missing/{action}', json={field:True}).status_code, 404)
                self.assertEqual(client.put(f"/company-sources/{source['id']}/{action}", json={field:True}, headers={'origin':'https://outside.example'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
