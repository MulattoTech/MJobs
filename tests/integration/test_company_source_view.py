"""Source-specific confidence, provenance, and explicit operator controls."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery import sources
from services.company_discovery.domains import record_company_domain
from services.discovery.company_feed import import_company_feed_jobs
from services.company_discovery.capabilities import feed_capabilities, fixture_source_capabilities
from services.discovery.pipeline import discover
from fixtures.mock_server import fixture_server
from services.discovery.sources.collect import configure_source
from services.discovery.sources.contract import CollectionStrategy, SourceCapabilities, SourceCapability
from services.discovery.sources.registry import SourceAdapterRegistry
from tests.integration.test_spine import PROFILE


class CompanySourceViewTests(unittest.TestCase):
    def _new_store(self):
        store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        store.migrate()
        return store

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = self._new_store()
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

    def test_application_counts_are_company_and_profile_scoped(self):
        source = self.source()
        profile_two = copy.deepcopy(PROFILE)
        profile_two['id'] = 'fixture-two'
        self.store.save_profile(profile_two)
        self.job(source, 'application-job', 'application-posting')
        with self.store.engine.begin() as c:
            upsert(c, m.companies, {'id':'other-company', 'name':'Other Company', 'domain':''})
            upsert(c, m.jobs, {'id':'other-application-job', 'company_id':'other-company',
                'requisition':'other-application-job', 'title':'Engineer', 'description':'Python',
                'skills':['Python'], 'location':'Unknown', 'remote_type':'UNKNOWN', 'attributes':{}})
            upsert(c, m.routes, {'id':'application-route', 'job_id':'application-job',
                'company_id':'acme', 'provider':'rss', 'tenant':'acme', 'requisition':'application-job',
                'url':'https://careers.example/jobs/application-job'})
            upsert(c, m.routes, {'id':'other-application-route', 'job_id':'other-application-job',
                'company_id':'other-company', 'provider':'rss', 'tenant':'other',
                'requisition':'other-application-job', 'url':'https://other.example/jobs/1'})
            for profile_id in ('fixture', 'fixture-two'):
                upsert(c, m.applications, {'id':'application-'+profile_id, 'job_id':'application-job',
                    'route_id':'application-route', 'profile_id':profile_id,
                    'profile_version':self.store.profile(profile_id)['version'], 'state':'PREPARED',
                    'target':'MOCK', 'fact_ids':[], 'trace':[], 'created_at':'2026-10-08T00:00:00Z'})
            upsert(c, m.applications, {'id':'application-other-company', 'job_id':'other-application-job',
                'route_id':'other-application-route', 'profile_id':'fixture',
                'profile_version':self.store.profile('fixture')['version'], 'state':'PREPARED',
                'target':'MOCK', 'fact_ids':[], 'trace':[], 'created_at':'2026-10-08T00:00:01Z'})
        self.assertEqual(self.view('fixture')[0]['applications'], 1)
        self.assertEqual(self.view('fixture-two')[0]['applications'], 1)
        self.assertEqual(self.view()[0]['applications'], 2)
        with TestClient(self.app) as client:
            response = client.get('/company-sources/view?profile_id=fixture')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['company_sources'][0]['applications'], 1)

    def test_import_activity_records_distinct_matches_for_current_profile(self):
        source = self.source()
        profile_two = copy.deepcopy(PROFILE)
        profile_two['id'] = 'fixture-two'
        self.store.save_profile(profile_two)
        imported = import_company_feed_jobs(self.store, company_id='acme', source_id=source['id'],
            provider='rss', tenant='acme', endpoint=source['feed_url'], strategy='RSS',
            items=[{'guid':'one','title':'Python Engineer','description':'Python','link':'/careers/one'},
                   {'guid':'two','title':'Python Engineer','description':'Python','link':'/careers/two'},
                   {'guid':'three','title':'Other Role','description':'Other','link':'/careers/three'}],
            profile_id='fixture', extra_profile_ids=['fixture-two'])
        self.assertEqual(imported, 3)
        with self.store.engine.connect() as c:
            event = c.execute(select(m.audit).where(
                m.audit.c.action == 'COMPANY_FEED_IMPORT_COMPLETE',
                m.audit.c.entity_id == 'acme').order_by(m.audit.c.timestamp.desc())).mappings().first()
        self.assertEqual(event['payload']['matched_jobs'], 2)
        self.assertEqual(event['payload']['evaluated_profile_count'], 2)
        activity = self.view()[0]['provenance']['events']
        self.assertEqual(next(item for item in activity if item['action'] == 'COMPANY_FEED_IMPORT_COMPLETE')['details']['matched_jobs'], 2)
        import_company_feed_jobs(self.store, company_id='acme', source_id=source['id'],
            provider='rss', tenant='acme', endpoint=source['feed_url'], strategy='RSS',
            items=[{'guid':'one','title':'Python Engineer','description':'Python','link':'/careers/one'},
                   {'guid':'two','title':'Python Engineer','description':'Python','link':'/careers/two'}],
            profile_id='fixture', extra_profile_ids=['fixture-two'])
        latest = next(item for item in self.view()[0]['provenance']['events']
                      if item['action'] == 'COMPANY_FEED_IMPORT_COMPLETE')
        self.assertEqual(latest['details']['matched_jobs'], 2)
        import_company_feed_jobs(self.store, company_id='acme', source_id=source['id'],
            provider='rss', tenant='acme', endpoint=source['feed_url'], strategy='RSS',
            items=[{'guid':'three','title':'Other Role','description':'Other','link':'/careers/three'}])
        latest = next(item for item in self.view()[0]['provenance']['events']
                      if item['action'] == 'COMPANY_FEED_IMPORT_COMPLETE')
        self.assertNotIn('matched_jobs', latest['details'])

        old_version = self.store.profile('fixture')['version']
        changed = copy.deepcopy(PROFILE)
        changed['facts'] = [*changed['facts'], {'id':'stale-check','text':'Windows', 'kind':'skill',
            'source':'fictional-test-profile', 'verified':True, 'user_approved':True, 'confidence':100}]
        self.store.save_profile(changed)
        self.assertNotEqual(old_version, self.store.profile('fixture')['version'])
        import_company_feed_jobs(self.store, company_id='acme', source_id=source['id'],
            provider='rss', tenant='acme', endpoint=source['feed_url'], strategy='RSS',
            items=[{'guid':'stale','title':'Python Engineer','description':'Python','link':'/careers/stale'}],
            profile_id='fixture', profile_version=old_version)
        latest = next(item for item in self.view()[0]['provenance']['events']
                      if item['action'] == 'COMPANY_FEED_IMPORT_COMPLETE')
        self.assertNotIn('matched_jobs', latest['details'])

        duplicate = {'guid':'same-guid','title':'Python Engineer','description':'Python','link':'/careers/same'}
        import_company_feed_jobs(self.store, company_id='acme', source_id=source['id'],
            provider='rss', tenant='acme', endpoint=source['feed_url'], strategy='RSS',
            items=[duplicate, duplicate], profile_id='fixture')
        latest = next(item for item in self.view()[0]['provenance']['events']
                      if item['action'] == 'COMPANY_FEED_IMPORT_COMPLETE')
        self.assertEqual(latest['details']['matched_jobs'], 1)
        self.assertEqual(latest['details']['jobs_imported'], 2)

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

    def test_feed_capabilities_survive_api_readback_and_disabled_legacy_sources(self):
        rss = self.source()
        json_feed = self.source('json', provider='json_feed', collection_strategy='PUBLIC_JSON')
        legacy = self.source('legacy', tenant_identifier='legacy-tenant', collection_strategy='PUBLIC_JSON')
        unsupported = self.source('html', provider='unsupported', collection_strategy='PUBLIC_HTML')
        sources.set_company_source_enabled(self.store, rss['id'], enabled=False)
        reopened = Store(self.store.url)
        try:
            with TestClient(create_app(reopened, 'http://127.0.0.1:8090', self.root,
                                       operation_mode='FIXTURE')) as client:
                response = client.get('/company-sources/view')
                self.assertEqual(response.status_code, 200, response.text)
                rows = {row['id']: row for row in response.json()['company_sources']}
            self.assertEqual(rows[rss['id']]['capabilities'], feed_capabilities('RSS').as_dict())
            self.assertFalse(rows[rss['id']]['enabled'])
            for source in (json_feed, legacy):
                self.assertEqual(rows[source['id']]['capabilities'], feed_capabilities('PUBLIC_JSON').as_dict())
            self.assertTrue(rows[json_feed['id']]['monitoring_supported'])
            self.assertFalse(rows[legacy['id']]['monitoring_supported'])
            self.assertEqual(rows[unsupported['id']]['capabilities'], feed_capabilities(None).as_dict())
            self.assertEqual(reopened.count('company_source_monitoring'), 0)
            with reopened.engine.connect() as connection:
                self.assertEqual(list(connection.execute(select(m.operations.c.id)).scalars()), [])
        finally:
            reopened.engine.dispose()

    def test_real_feed_imports_record_the_declaration_and_supported_job_fields(self):
        for provider, strategy in (('rss', 'RSS'), ('json_feed', 'PUBLIC_JSON')):
            with self.subTest(provider=provider):
                source = self.source('declaration-'+provider, provider=provider, collection_strategy=strategy)
                item = {'id':provider+'-one', 'guid':provider+'-one', 'title':'Python Engineer',
                        'description':'Remote Python role. Salary $120,000 - $150,000 per year.',
                        'location':'Remote, United States',
                        'link':'https://careers.example/jobs/'+provider,
                        'url':'https://careers.example/jobs/'+provider}
                self.assertEqual(import_company_feed_jobs(self.store, company_id='acme', source_id=source['id'],
                    provider=provider, tenant='acme', endpoint=source['endpoint'], strategy=strategy,
                    items=[item], profile_id='fixture'), 1)
                with self.store.engine.connect() as connection:
                    posting = connection.execute(select(m.postings).where(
                        m.postings.c.source=='company-feed:'+source['id'])).mappings().one()
                    job = connection.execute(select(m.jobs).where(m.jobs.c.id==posting['job_id'])).mappings().one()
                    event = connection.execute(select(m.audit.c.payload).where(
                        m.audit.c.action=='COMPANY_FEED_IMPORT_COMPLETE',
                        m.audit.c.payload['company_source_id'].as_string()==source['id'])).scalar_one()
                row = next(row for row in self.view() if row['id']==source['id'])
                self.assertEqual(event['source_capabilities'], row['capabilities'])
                self.assertEqual(row['jobs_found'], 1)
                self.assertIn('Remote Python role.', job['description'])
                self.assertEqual(job['attributes']['salary_min'], 120000)
                self.assertEqual(job['attributes']['salary_max'], 150000)
                self.assertTrue(job['attributes']['remote_indicated'])
                self.assertEqual(posting['application_url'], item['url'])
                self.assertFalse(row['capabilities']['direct_application'])
                self.assertFalse(row['capabilities']['application_status'])

    def test_local_fixture_discovery_declares_its_actual_source_and_read_only_scope(self):
        with fixture_server() as origin:
            discover(self.store, origin, 'fixture')
        with TestClient(self.app) as client:
            response = client.get('/company-sources/view')
            self.assertEqual(response.status_code, 200, response.text)
            rows = response.json()['company_sources']
        fixtures = [row for row in rows if row['provider']=='mock-ats']
        self.assertTrue(fixtures)
        for row in fixtures:
            self.assertEqual(row['collection_strategy'], 'FIXTURE')
            self.assertEqual(row['capabilities'], fixture_source_capabilities().as_dict())
            self.assertFalse(row['monitoring_supported'])
        self.assertGreater(self.store.count('canonical_jobs'), 0)
        with self.store.engine.connect() as connection:
            event = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.action=='MOCK_DISCOVERY_COMPLETE')).scalar_one()
        self.assertEqual(event['source_capabilities'], fixtures[0]['capabilities'])

    def test_linked_builtin_and_extension_declarations_reopen_without_factories(self):
        extension_declaration = SourceCapabilities(CollectionStrategy.PUBLIC_JSON,
                                                   frozenset({SourceCapability.DISCOVERY}))
        class Extension:
            def capabilities(self):
                return extension_declaration
        registry = SourceAdapterRegistry([('fixture_extension', lambda source, **kwargs: Extension())])
        expected = {}
        for provider in registry.providers():
            tenant = 'global:acme' if provider == 'lever' else 'acme'
            identifier = configure_source(self.store, tenant, provider=provider, registry=registry)
            with self.store.engine.connect() as connection:
                collector = dict(connection.execute(select(m.job_sources).where(
                    m.job_sources.c.id == identifier)).mappings().one())
            source = self.source('linked-'+provider, provider=provider, tenant_identifier=tenant,
                                 collection_strategy=collector['strategy'], discovered_automatically=False)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_sources).where(m.company_sources.c.id == source['id'])
                                   .values(collector_source_id=identifier))
            expected[source['id']] = collector['capabilities']
        reopened = Store(self.store.url)
        try:
            with patch('services.discovery.sources.registry.SourceAdapterRegistry.create',
                       side_effect=AssertionError('Readback must not construct an adapter')):
                with TestClient(create_app(reopened, 'http://127.0.0.1:8090', self.root,
                                           operation_mode='FIXTURE')) as client:
                    response = client.get('/company-sources/view')
                    self.assertEqual(response.status_code, 200, response.text)
                    rows = response.json()['company_sources']
            self.assertEqual({row['id']:row['capabilities'] for row in rows}, expected)
            self.assertFalse(next(row for row in rows if row['provider']=='wwr')['capabilities']['salary'])
            self.assertEqual(next(row for row in rows if row['provider']=='fixture_extension')['capabilities'],
                             extension_declaration.as_dict())
        finally:
            reopened.engine.dispose()

    def test_controls_require_strict_boolean_and_existing_source(self):
        source=self.source()
        with TestClient(self.app) as client:
            for action, field in [('pin','pinned'), ('enabled','enabled')]:
                for bad in ['true', 1, None]:
                    self.assertEqual(client.put(f"/company-sources/{source['id']}/{action}", json={field:bad}).status_code, 422)
                self.assertEqual(client.put(f'/company-sources/missing/{action}', json={field:True}).status_code, 404)
                self.assertEqual(client.put(f"/company-sources/{source['id']}/{action}", json={field:True}, headers={'origin':'https://outside.example'}).status_code, 403)


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'),
                     'Set MJOBS_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database')
class CompanySourceCapabilityPostgresTests(unittest.TestCase):
    from tests.integration.test_lever_form_review_flow import LeverFormReviewPostgresFlowTests as _Fixture
    _new_store = _Fixture._new_store
    _drop_schema = _Fixture._drop_schema
    setUp = CompanySourceViewTests.setUp
    tearDown = CompanySourceViewTests.tearDown
    source = CompanySourceViewTests.source
    view = CompanySourceViewTests.view
    test_feed_capabilities_survive_api_readback_and_disabled_legacy_sources = (
        CompanySourceViewTests.test_feed_capabilities_survive_api_readback_and_disabled_legacy_sources)
    test_linked_builtin_and_extension_declarations_reopen_without_factories = (
        CompanySourceViewTests.test_linked_builtin_and_extension_declarations_reopen_without_factories)
    test_real_feed_imports_record_the_declaration_and_supported_job_fields = (
        CompanySourceViewTests.test_real_feed_imports_record_the_declaration_and_supported_job_fields)
    test_local_fixture_discovery_declares_its_actual_source_and_read_only_scope = (
        CompanySourceViewTests.test_local_fixture_discovery_declares_its_actual_source_and_read_only_scope)


if __name__ == '__main__':
    unittest.main()
