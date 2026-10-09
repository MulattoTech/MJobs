"""Public company discovery identity and worker boundaries, with controlled responses."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.public_resolution import resolve_public_company
from services.company_discovery.resolution import resolve_company
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.domains import record_company_domain
from services.company_discovery.sources import set_company_source_enabled
from services.audit_history import list_audit_history
from workers.operations import OperationCancelled
from services.company_discovery.triggers import queue_company_discovery
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class PublicCompanyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.url = f"sqlite:///{(self.root / 'public.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(PROFILE)
        self.store.save_profile({**PROFILE, 'id': 'other', 'name': 'Other Profile',
                                 'email': 'other@example.invalid'})
        self.candidate = {'company_name':'Example Systems','company_website':'https://employer.example/'}
        self.env = {'MJOBS_DATABASE_URL':self.url,'MJOBS_OPERATION_MODE':'PERSONAL',
                    'MJOBS_REDIS_URL':'redis://127.0.0.1:1/0','MJOBS_OUTPUT_DIR':str(self.root/'outputs')}

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_conflicting_feed_id_preflight_records_failure_without_partial_jobs(self):
        first = {'id': 'repeated', 'title': 'First title', 'url': 'https://employer.example/jobs/1'}
        pages = {
            '/robots.txt': 'User-agent: *\nAllow: /',
            '/': '<a rel="careers" href="/careers">Careers</a>',
            '/careers': '<link rel="alternate" type="application/feed+json" href="/jobs.json">',
            '/jobs.json': json.dumps({'version': 'https://jsonfeed.org/version/1.1', 'items': [first, dict(first, title='Conflicting title')]}),
        }
        def fetch(client, url, deadline):
            request = httpx.Request('GET', url)
            return httpx.Response(200, text=pages[request.url.path], request=request)
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once', fetch):
            queued = queue_company_discovery(self.store, self.candidate, 'USER_ADDED_COMPANY', 'fixture', public=True)
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'DEAD_LETTERED', result)
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == queued['operation_id'])).mappings().one()
        self.assertEqual(operation['state'], 'DEAD_LETTERED')
        self.assertEqual(operation['error'], 'ValueError')
        self.assertIsNotNone(operation['dead_lettered_at'])
        self.assertEqual(self.store.count('canonical_jobs'), 0)
        self.assertEqual(self.store.count('source_application_link_observations'), 0)
        with self.store.engine.connect() as connection:
            observation = connection.execute(select(m.source_quality_observations)).mappings().one()
        self.assertFalse(observation['succeeded'])
        self.assertTrue(observation['available'])
        self.assertEqual(observation['enumerated'], 2)
        self.assertIsNone(observation['accepted'])

    def test_claimed_name_and_tenant_cannot_bind_public_site_to_an_existing_company(self):
        with self.store.engine.begin() as connection:
            upsert(connection,m.companies,{'id':'existing','name':'Example Systems','domain':''})
        resolved=resolve_public_company(self.store,{**self.candidate,'ats_tenant':'claimed'})
        self.assertNotEqual(resolved['company_id'],'existing')
        self.assertEqual(resolved['confidence'],0.45)
        self.assertEqual(resolved['evidence'],['CANDIDATE_DOMAIN_UNVERIFIED'])
        same=resolve_public_company(self.store,{**self.candidate,'company_website':'https://employer.example/about'})
        self.assertEqual(same['company_id'],resolved['company_id'])

    def test_public_worker_imports_feed_without_claiming_verified_identity_or_ats(self):
        seen=[]
        pages={
            '/robots.txt':'User-agent: *\nAllow: /',
            '/':'<html>Example Systems<a rel="careers" href="/careers">Careers</a></html>',
            '/careers':'<html>Example Systems<link rel="alternate" type="application/feed+json" href="/jobs.json"></html>',
            '/jobs.json':'{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"role-1","url":"https://employer.example/jobs/1","title":"Python Engineer","content_text":"Python work. $90,000 per year.","location":"Remote - United States"}]}',
            '/jobs/1':'<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","identifier":{"@type":"PropertyValue","value":"role-1"},"title":"Python Engineer","url":"https://employer.example/jobs/1"}</script>',
        }
        def fetch(client,url,deadline):
            seen.append(url)
            req=httpx.Request('GET',url)
            content_type='application/json' if req.url.path.endswith('.json') else 'text/html; charset=utf-8'
            return httpx.Response(200,text=pages[req.url.path],headers={'content-type':content_type},request=req)
        with patch.dict(os.environ,self.env), patch.object(PublicCompanyClient,'_request_once',fetch):
            app=create_app(self.store,'http://127.0.0.1:18799',self.root/'outputs',redis_url='redis://127.0.0.1:1/0',operation_mode='PERSONAL')
            with TestClient(app) as client:
                response=client.post('/companies/discovery',json={**self.candidate,'reason':'USER_ADDED_COMPANY','profile_id':'fixture'})
                self.assertEqual(response.status_code,202,response.text)
                operation_id=response.json()['operation_id']
                result=execute_operation(operation_id)
                self.assertEqual(result['status'],'COMPLETED',result)
                history=client.get('/profiles/fixture/audit-history').json()['events']
                company_events=[event for event in history if event['type'] in {
                    'COMPANY_DISCOVERED','COMPANY_SOURCE_ENROLLED'}]
                self.assertEqual({event['type'] for event in company_events},
                                 {'COMPANY_DISCOVERED','COMPANY_SOURCE_ENROLLED'})
                self.assertEqual([event['type'] for event in company_events].count('COMPANY_DISCOVERED'),1)
                self.assertEqual([event['type'] for event in company_events].count('COMPANY_SOURCE_ENROLLED'),1)
                self.assertTrue(all(event['subject']=='Example Systems' for event in company_events))
                self.assertNotIn('employer.example',str(company_events))
                view=client.get('/company-sources/view',params={'profile_id':'fixture'}).json()['company_sources']
                self.assertEqual(len(view),1)
                self.assertEqual(view[0]['status'],'COMPANY_SOURCE_REVIEW_REQUIRED')
                link_verification=view[0]['quality']['link_verification']
                self.assertEqual(link_verification['sampled_count'],1)
                self.assertEqual(link_verification['accepted_posting_count'],1)
                self.assertEqual(link_verification['conclusive_sample_count'],1)
                self.assertEqual(link_verification['accurate_sample_count'],1)
                self.assertEqual(link_verification['unknown_sample_count'],0)
                with self.store.engine.connect() as connection:
                    initial=connection.execute(select(m.company_sources)).mappings().one()
                    self.assertFalse(initial['enabled'])
                    source_id=initial['id']
                set_company_source_enabled(self.store,source_id,enabled=True)
                retry=queue_company_discovery(self.store,self.candidate,'USER_ADDED_COMPANY','fixture',public=True,request_id='explicit-rediscovery')
                self.assertEqual(retry['status'],'QUEUED')
                self.assertEqual(execute_operation(retry['operation_id'])['status'],'COMPLETED')
                repeated=list_audit_history(self.store,'fixture')['events']
                repeated_company_events=[event for event in repeated if event['type'] in {
                    'COMPANY_DISCOVERED','COMPANY_SOURCE_ENROLLED'}]
                self.assertEqual({event['type'] for event in repeated_company_events},
                                 {'COMPANY_DISCOVERED','COMPANY_SOURCE_ENROLLED'})
                self.assertEqual(len(repeated_company_events),2)
                self.assertFalse(any(event['type'] in {'COMPANY_DISCOVERED','COMPANY_SOURCE_ENROLLED'}
                                     for event in list_audit_history(self.store,'other')['events']))
        self.assertEqual(self.store.count('canonical_jobs'),1)
        self.assertEqual(self.store.count('company_ats_tenants'),0)
        with self.store.engine.connect() as connection:
            source=connection.execute(select(m.company_sources)).mappings().one()
            self.assertTrue(source['enabled'], 'Explicit enable preference survives rediscovery without verifying identity')
            self.assertEqual(source['confidence'],0.45)
            self.assertTrue(all(not row['verified'] for row in connection.execute(select(m.company_domains)).mappings()))
        self.assertEqual(set(seen),{'https://employer.example'+p for p in pages})
        self.assertGreaterEqual(seen.count('https://employer.example/jobs/1'),1)

    def test_personal_worker_reuses_verified_domain_when_discovery_website_is_omitted(self):
        candidate = {'company_name': 'Example Systems'}
        resolved = resolve_company(self.store, candidate)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                'id': resolved['company_id'], 'name': 'Example Systems', 'domain': '',
            })
        record_company_domain(
            self.store, resolved['company_id'], 'employer.example',
            'FIRST_PARTY_JOB_LINK', ['verified first-party careers posting'],
        )
        seen = []
        pages = {
            '/robots.txt': 'User-agent: *\\nAllow: /',
            '/': '<html>Example Systems<a rel="careers" href="/careers">Careers</a></html>',
            '/careers': '<html>Example Systems<link rel="alternate" type="application/feed+json" href="/jobs.json"></html>',
            '/jobs.json': '{"version":"https://jsonfeed.org/version/1.1","items":[]}',
        }

        def fetch(client, url, deadline):
            seen.append(url)
            request = httpx.Request('GET', url)
            return httpx.Response(200, text=pages[request.url.path], request=request)

        queued = queue_company_discovery(
            self.store, candidate, 'USER_ADDED_COMPANY', profile_id='fixture', public=True,
        )
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once', fetch):
            result = execute_operation(queued['operation_id'])

        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertTrue(seen)
        self.assertTrue(all(url.startswith('https://employer.example') for url in seen))

    def test_stale_fixture_operation_remains_blocked_in_personal_mode(self):
        with patch.dict(os.environ,self.env):
            queued=queue_company_discovery(self.store,self.candidate,'USER_ADDED_COMPANY')
            with patch.object(PublicCompanyClient,'get',side_effect=AssertionError('No egress allowed')):
                result=execute_operation(queued['operation_id'])
        self.assertEqual(result['error_category'],'FIXTURE_OPERATION_DISABLED')

    def test_production_page_cannot_promote_mock_ats_markup(self):
        pages={'/robots.txt':'User-agent: *\nAllow: /','/':'Example Systems<a rel="careers" href="/careers">Careers</a>',
               '/sitemap.xml':'<urlset/>',
               '/careers':'<a data-provider="mock-ats" data-tenant="fake" href="/fake">Jobs</a>'}
        # The public discovery policy also checks its bounded same-origin
        # fallback routes before rejecting unsupported mock-only ATS markup.
        pages.update({path: 'Example Systems' for path in (
            '/jobs', '/careers/jobs', '/jobs/search', '/work-with-us', '/join-us',
            '/join-our-team', '/employment', '/opportunities')})
        def fetch(client,url,deadline):
            req=httpx.Request('GET',url)
            return httpx.Response(200,text=pages[req.url.path],request=req)
        with patch.dict(os.environ,self.env),patch.object(PublicCompanyClient,'_request_once',fetch):
            queued=queue_company_discovery(self.store,self.candidate,'USER_ADDED_COMPANY',
                                           profile_id='fixture',public=True)
            result=execute_operation(queued['operation_id'])
        self.assertEqual(result['status'],'BLOCKED',result)
        self.assertEqual(self.store.count('company_job_sources'),0)
        self.assertEqual(self.store.count('company_ats_tenants'),0)
        self.assertFalse(any(event['type'] in {'COMPANY_DISCOVERED','COMPANY_SOURCE_ENROLLED'}
                             for event in list_audit_history(self.store,'fixture')['events']))

    def test_verified_domain_is_required_for_existing_company_binding_and_ties_need_review(self):
        with self.store.engine.begin() as connection:
            for name in ('verified','second'):
                upsert(connection,m.companies,{'id':name,'name':name,'domain':''})
        record_company_domain(self.store,'verified','employer.example','FIRST_PARTY_JOB_LINK',['Independent employer job evidence'])
        self.assertEqual(resolve_public_company(self.store,self.candidate)['company_id'],'verified')
        record_company_domain(self.store,'second','employer.example','FIRST_PARTY_JOB_LINK',['Conflicting evidence'])
        with self.assertRaisesRegex(Exception,'COMPANY_IDENTITY_REVIEW_REQUIRED'):
            resolve_public_company(self.store,self.candidate)

    def test_invalid_public_target_rejected_by_api_without_queue_or_network(self):
        with patch.dict(os.environ,self.env):
            app=create_app(self.store,'http://127.0.0.1:18799',self.root/'outputs',redis_url='redis://127.0.0.1:1/0',operation_mode='PERSONAL')
            with TestClient(app) as client, patch('socket.getaddrinfo',side_effect=AssertionError('API must not perform DNS')):
                for target in ('http://employer.example/','https://localhost/','https://employer.example:8443/','https://user:secret@employer.example/'):
                    response=client.post('/companies/discovery',json={**self.candidate,'company_website':target,'reason':'USER_ADDED_COMPANY'})
                    self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.store.count(m.operations.name),0)

    def test_robots_denial_and_cancellation_are_distinct_durable_states(self):
        def deny(client,url,deadline):
            return httpx.Response(200,text='User-agent: *\nDisallow: /',request=httpx.Request('GET',url))
        with patch.dict(os.environ,self.env),patch.object(PublicCompanyClient,'_request_once',deny):
            queued=queue_company_discovery(self.store,self.candidate,'USER_ADDED_COMPANY',public=True)
            result=execute_operation(queued['operation_id'])
            self.assertEqual(result['status'],'BLOCKED')
            self.assertEqual(result['error_category'],'ROBOTS_DENIED')
            with self.store.engine.begin() as connection:
                connection.execute(m.operations.update().where(m.operations.c.id==queued['operation_id']).values(state='QUEUED'))
            with patch('workers.tasks.check_cancelled',side_effect=OperationCancelled()):
                result=execute_operation(queued['operation_id'])
            self.assertEqual(result['status'],'CANCELLED')
            self.assertEqual(self.store.count('canonical_jobs'),0)

    def test_explicit_retry_is_fresh_but_replayed_request_stays_idempotent(self):
        first=queue_company_discovery(self.store,self.candidate,'USER_ADDED_COMPANY',public=True,request_id='first')
        replay=queue_company_discovery(self.store,self.candidate,'USER_ADDED_COMPANY',public=True,request_id='first')
        retry=queue_company_discovery(self.store,self.candidate,'USER_ADDED_COMPANY',public=True,request_id='second')
        self.assertEqual(first['operation_id'],replay['operation_id'])
        self.assertNotEqual(first['operation_id'],retry['operation_id'])

if __name__=='__main__':unittest.main()
