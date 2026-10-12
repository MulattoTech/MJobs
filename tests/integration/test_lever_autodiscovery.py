"""Actual operation/collector persistence for reviewed and verified Lever employers."""
import os
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.domains import record_company_domain
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.requests import approve_discovery_request
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.scheduler import schedule_due_sources
from services.discovery.sources.collect import collect_source
from services.discovery.sources.lever import Lever, endpoint
from services.discovery.sources.errors import SourceError
from tests.integration import test_reviewed_greenhouse_enrollment as fixture
from tests.unit.test_lever_source import posting
from workers.tasks import execute_operation


class LeverAutodiscoveryTests(unittest.TestCase):
    def setUp(self):
        fixture.ReviewedGreenhouseEnrollmentTests.setUp(self)
        self.region = 'global'
        self.site = 'example'
        self.jobs = [posting()]
        self.during_fetch = None

    def tearDown(self):
        fixture.ReviewedGreenhouseEnrollmentTests.tearDown(self)

    @property
    def tenant(self):
        return self.region+':'+self.site

    def collect(self):
        request = fixture.ReviewedGreenhouseEnrollmentTests.collect(self)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id=='reviewed-employer-original-route')
                .values(provider='lever',tenant=self.site,requisition=self.jobs[0]['id'],url=self.jobs[0]['hostedUrl']))
        return request

    def page(self, client, url, deadline):
        self.fetches.append(url)
        req = httpx.Request('GET',url)
        host = 'jobs.eu.lever.co' if self.region=='eu' else 'jobs.lever.co'
        pages = {'/robots.txt':'User-agent: *\nAllow: /',
                 '/':'<a rel="careers" href="/careers">Careers</a>',
                 '/careers':f'<a href="https://{host}/{self.site}">Open jobs</a>'}
        return httpx.Response(200 if req.url.path in pages else 404,text=pages.get(req.url.path,''),request=req)

    def board(self, adapter, suffix, params=None):
        self.fetches.append(endpoint(adapter.tenant))
        if self.during_fetch:
            self.during_fetch()
        return self.jobs

    def execute(self, operation_id):
        with patch.dict(os.environ,self.env), \
                patch.object(PublicCompanyClient,'_request_once',lambda client,url,deadline:self.page(client,url,deadline)), \
                patch.object(Lever,'_get',lambda adapter,suffix,params=None:self.board(adapter,suffix,params)):
            return execute_operation(operation_id)

    def enroll(self, *, verified=False):
        request = self.collect()
        if verified:
            record_company_domain(self.store,self.company_id,'employer.example',
                                  'FIRST_PARTY_JOB_LINK',['synthetic verified employer link'])
        queued = approve_discovery_request(self.store,request['id'],'https://employer.example/',
            request['revision'],confirm_company_identity=not verified)
        result = self.execute(queued['operation_id'])
        self.assertEqual(result['status'],'COMPLETED',result)
        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.company_sources).where(
                m.company_sources.c.provider=='lever',m.company_sources.c.tenant==self.tenant)).mappings().one())
        return request,source

    def _assert_durable_chain(self, *, verified=False):
        request,source = self.enroll(verified=verified)
        self.assertEqual(source['company_id'],self.company_id)
        self.assertEqual(source['endpoint'],endpoint(self.tenant))
        self.assertEqual(source['identity_authorization'] is None,verified)
        from services.dedupe.route_redirects import RouteRedirectProbeClient
        with self.store.engine.connect() as connection:
            tenant = dict(connection.execute(select(m.company_ats_tenants).where(
                m.company_ats_tenants.c.id==source['ats_tenant_id'])).mappings().one())
        self.assertEqual(tenant['board_identifier'],self.site)
        self.assertEqual(tenant['organization_identifier'],self.region)
        probe = RouteRedirectProbeClient('https://aggregator.example/redirect',verified_domains=[],ats_tenants=[tenant])
        try:
            self.assertTrue(probe.allowed(self.jobs[0]['hostedUrl']))
            other_host='jobs.lever.co' if self.region=='eu' else 'jobs.eu.lever.co'
            self.assertFalse(probe.allowed(posting(host=other_host)['hostedUrl']))
        finally:
            probe.close()
        if not verified:
            self.assertEqual(source['identity_authorization']['request_id'],request['id'])
            self.assertEqual(self.store.count('company_domains'),0)
        # The aggregator and first-party board point to one original canonical job.
        with self.store.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(m.jobs.c.id).where(
                m.jobs.c.company_id==self.company_id)).scalars().all()),1)
            native_route=connection.execute(select(m.routes).where(m.routes.c.provider=='lever',
                m.routes.c.tenant==self.tenant,m.routes.c.requisition==self.jobs[0]['id'])).mappings().one_or_none()
            self.assertIsNotNone(native_route,'Deduped first-party posting must retain its verified regional route')
            from services.applications.adapters.lever import LeverApplicationAdapter
            for route in connection.execute(select(m.routes).where(m.routes.c.provider=='lever')).mappings():
                self.assertIsNotNone(LeverApplicationAdapter().normalize_route(route),dict(route))
        reopened = Store(self.store.url)
        try:
            with reopened.engine.begin() as connection:
                connection.execute(update(m.job_sources).where(m.job_sources.c.id==source['collector_source_id'])
                                   .values(last_attempt='2000-01-01T00:00:00+00:00'))
            schedule = schedule_due_sources(reopened)
            operation = next(row for row in schedule['scheduled'] if row['source_id']==source['collector_source_id'])
            host = 'jobs.eu.lever.co' if self.region=='eu' else 'jobs.lever.co'
            self.jobs.append(posting(2,host=host))
            result = self.execute(operation['operation_id'])
            self.assertEqual(result['status'],'COMPLETED',result)
            with reopened.engine.connect() as connection:
                jobs = connection.execute(select(m.jobs).where(m.jobs.c.company_id==self.company_id)).mappings().all()
                self.assertEqual(len(jobs),2)
                self.assertEqual({row['company_id'] for row in jobs},{self.company_id})
                for route in connection.execute(select(m.routes).where(m.routes.c.provider=='lever')).mappings():
                    self.assertIsNotNone(LeverApplicationAdapter().normalize_route(route),dict(route))
        finally:
            reopened.engine.dispose()

    def test_reviewed_global_import_dedupe_reload_and_recurrence(self):
        self._assert_durable_chain()

    def test_reviewed_eu_import_dedupe_reload_and_recurrence(self):
        self.region='eu';self.jobs=[posting(host='jobs.eu.lever.co')]
        self._assert_durable_chain()

    def test_verified_employer_import_reload_and_recurrence(self):
        self._assert_durable_chain(verified=True)

    def test_review_revocation_blocks_scheduler_and_fetch(self):
        request,source = self.enroll()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id==request['id']).values(domain_evidence=[]))
            connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
        self.assertFalse(any(row['source_id']==source['collector_source_id']
                             for row in schedule_due_sources(self.store)['scheduled']))
        with patch.object(Lever,'_get',side_effect=AssertionError('Revoked source must not fetch')):
            with self.assertRaisesRegex(Exception,'DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED'):
                collect_source(self.store,source['collector_source_id'],'fixture')

    def test_revocation_during_fetch_cannot_import_new_posting(self):
        request,source = self.enroll()
        self.jobs.append(posting(2))
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id==source['collector_source_id'])
                               .values(last_attempt='2000-01-01T00:00:00+00:00'))
        def revoke():
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_discovery_requests).where(
                    m.company_discovery_requests.c.id==request['id']).values(domain_evidence=[]))
        self.during_fetch=revoke
        with patch.object(Lever,'_get',lambda adapter,suffix,params=None:self.board(adapter,suffix,params)):
            with self.assertRaisesRegex(Exception,'DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED'):
                collect_source(self.store,source['collector_source_id'],'fixture')
        with self.store.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(m.jobs.c.id).where(m.jobs.c.company_id==self.company_id)).all()),1)
            collector=connection.execute(select(m.job_sources).where(m.job_sources.c.id==source['collector_source_id'])).mappings().one()
            self.assertEqual(collector['consecutive_failures'],0)

    def test_paused_legacy_manual_source_is_not_replaced_or_enabled(self):
        request=self.collect()
        old=upsert_company_job_source(self.store,company_id=self.company_id,provider='lever',
            tenant_identifier='example',career_url='https://employer.example/careers',
            public_api_url=endpoint(self.tenant),collection_strategy='API',enabled=False)
        queued=approve_discovery_request(self.store,request['id'],'https://employer.example/',
            request['revision'],confirm_company_identity=True)
        result=self.execute(queued['operation_id'])
        self.assertNotEqual(result['status'],'COMPLETED',result)
        with self.store.engine.connect() as connection:
            row=connection.execute(select(m.company_sources).where(m.company_sources.c.id==old['id'])).mappings().one()
            self.assertFalse(row['enabled'])
            self.assertEqual(connection.execute(select(m.job_sources.c.id).where(m.job_sources.c.provider=='lever')).all(),[])

    def test_source_pause_blocks_recollection(self):
        _,source=self.enroll()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_sources).where(m.company_sources.c.id==source['id']).values(enabled=False))
        with patch.object(Lever,'_get',side_effect=AssertionError('Paused source must not fetch')):
            with self.assertRaisesRegex(Exception,'COMPANY_SOURCE_DISABLED'):
                collect_source(self.store,source['collector_source_id'],'fixture')

    def test_same_slug_regions_have_distinct_collector_identities(self):
        from services.discovery.sources.collect import configure_source
        first=configure_source(self.store,'global:example',provider='lever')
        second=configure_source(self.store,'eu:example',provider='lever')
        self.assertNotEqual(first,second)

    def test_foreign_board_owner_blocks_enrollment_without_collector_or_reparenting(self):
        request=self.collect()
        with self.store.engine.begin() as connection:
            upsert(connection,m.companies,{'id':'foreign-employer','name':'Other Employer','domain':''})
        old=upsert_company_job_source(self.store,company_id='foreign-employer',provider='lever',
            tenant_identifier=self.tenant,career_url='https://other.example/careers',
            public_api_url=endpoint(self.tenant),collection_strategy='API',enabled=True)
        queued=approve_discovery_request(self.store,request['id'],'https://employer.example/',
            request['revision'],confirm_company_identity=True)
        result=self.execute(queued['operation_id'])
        self.assertNotEqual(result['status'],'COMPLETED',result)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.company_sources.c.company_id).where(
                m.company_sources.c.id==old['id'])).scalar_one(),'foreign-employer')
            self.assertEqual(connection.execute(select(m.job_sources.c.id).where(m.job_sources.c.provider=='lever')).all(),[])
            self.assertEqual(connection.execute(select(m.jobs.c.company_id)).scalar_one(),self.company_id)

    def test_region_binding_change_blocks_egress(self):
        _,source=self.enroll()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_sources).where(m.company_sources.c.id==source['id'])
                               .values(endpoint=endpoint('eu:example')))
            connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
        with patch.object(Lever,'_get',side_effect=AssertionError('Changed binding must not fetch')):
            with self.assertRaisesRegex(Exception,'DISCOVERY_.*CHANGED|COMPANY_ATS_BINDING_REVIEW_REQUIRED'):
                collect_source(self.store,source['collector_source_id'],'fixture')

    def test_later_inventory_failure_preserves_existing_jobs_and_recovery_imports_once(self):
        _,source=self.enroll()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
            original=[dict(row) for row in connection.execute(select(m.jobs)).mappings()]
            snapshots=connection.execute(select(m.posting_snapshots.c.id)).scalars().all()
        def pages(adapter,suffix,params=None):
            if params['skip']==0:
                return [posting(i+1) for i in range(100)]
            raise SourceError('SOURCE_HTTP_503')
        with patch.object(Lever,'_get',pages),self.assertRaisesRegex(SourceError,'SOURCE_HTTP_503'):
            collect_source(self.store,source['collector_source_id'],'fixture')
        with self.store.engine.begin() as connection:
            self.assertEqual([dict(row) for row in connection.execute(select(m.jobs)).mappings()],original)
            self.assertEqual(connection.execute(select(m.posting_snapshots.c.id)).scalars().all(),snapshots)
            connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00',backoff_until=None))
        self.jobs.append(posting(2))
        with patch.object(Lever,'_get',lambda adapter,suffix,params=None:self.board(adapter,suffix,params)):
            collect_source(self.store,source['collector_source_id'],'fixture')
        with self.store.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(m.jobs.c.id)).all()),2)

    def test_native_job_with_custom_route_id_recollects_without_replacing_route(self):
        _,source=self.enroll()
        self.jobs.append(posting(2))
        def due():
            with self.store.engine.begin() as connection:
                connection.execute(update(m.job_sources).values(last_attempt='2000-01-01T00:00:00+00:00'))
        with patch.object(Lever,'_get',lambda adapter,suffix,params=None:self.board(adapter,suffix,params)):
            due()
            collect_source(self.store,source['collector_source_id'],'fixture')
            with self.store.engine.begin() as connection:
                original=dict(connection.execute(select(m.routes).where(m.routes.c.tenant==self.tenant,
                    m.routes.c.requisition==self.jobs[1]['id'])).mappings().one())
                self.assertEqual(original['id'],original['job_id'])
                original['url'] += '?source=preserved'
                connection.execute(update(m.routes).where(m.routes.c.id==original['id']).values(
                    id='preserved-custom-lever-route',url=original['url']))
            due()
            collect_source(self.store,source['collector_source_id'],'fixture')
        with self.store.engine.connect() as connection:
            route=dict(connection.execute(select(m.routes).where(m.routes.c.tenant==self.tenant,
                m.routes.c.requisition==self.jobs[1]['id'])).mappings().one())
        self.assertEqual(route,dict(original,id='preserved-custom-lever-route'))
