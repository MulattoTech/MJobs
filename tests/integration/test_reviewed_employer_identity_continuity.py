"""Explicit company identity confirmation binds a reviewed site to discovery."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.requests import (
    approve_discovery_request, authorize_discovery_request, list_discovery_requests,
)
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.sources import upsert_company_job_source
from services.company_discovery.domains import record_company_domain
from services.discovery.company_feed import import_company_feed_jobs
from services.discovery.sources.collect import collect_source, configure_source
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class ReviewedEmployerIdentityContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'review.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.store.save_profile(copy.deepcopy(PROFILE))
        self.source = configure_source(self.store, 'example')
        self.company_id = identity('greenhouse', 'example')
        self.env = {'MJOBS_DATABASE_URL': self.store.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
                    'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0', 'MJOBS_OUTPUT_DIR': str(self.root / 'outputs')}

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def collect(self):
        def response(request):
            return httpx.Response(200, json={'jobs': [RAW]} if request.url.path.endswith('/jobs')
                                  else {'name': 'Example Public Employer'})
        collect_source(self.store, self.source, 'fixture', transport=httpx.MockTransport(response))
        return list_discovery_requests(self.store)[0]

    def test_explicit_confirmation_records_auditable_mapping_and_uses_existing_company_id(self):
        request = self.collect()
        result = approve_discovery_request(self.store, request['id'], 'https://employer.example/careers',
                                           request['revision'], confirm_company_identity=True)
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == result['operation_id'])).mappings().one()
            audit = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.action == 'COMPANY_DISCOVERY_REQUEST_AUTHORIZED')).mappings().one()
            queued_request = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request['id'])).mappings().one()
        self.assertEqual(queued_request['authorization'], 'USER_CONFIRMED_DOMAIN')
        self.assertEqual(queued_request['domain_evidence'][0]['method'], 'USER_CONFIRMED_DOMAIN')
        self.assertEqual(queued_request['domain_evidence'][0]['request_id'], request['id'])
        self.assertEqual(queued_request['domain_evidence'][0]['reviewed_revision'], request['revision'])
        self.assertEqual(queued_request['domain_evidence'][0]['source_snapshot_sha256'],
                         audit['payload']['source_snapshot_sha256'])
        self.assertFalse(audit['payload']['independent_verification'])
        self.assertEqual(self.store.count('company_domains'), 0)
        self.assertEqual(operation['entity_id'], self.company_id)
        self.assertEqual(operation['params']['candidate']['company_website'], 'https://employer.example/careers')
        self.assertEqual(audit['payload']['authorization'], 'USER_CONFIRMED_DOMAIN')
        self.assertEqual(audit['payload']['website'], 'https://employer.example/careers')
        self.assertEqual(audit['payload']['reviewed_revision'], request['revision'])

    def test_default_and_false_confirmation_keep_candidate_identity_isolated(self):
        request = self.collect()
        result = approve_discovery_request(self.store, request['id'], 'https://employer.example/', request['revision'])
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == result['operation_id'])).mappings().one()
        self.assertEqual(operation['params']['discovery_request_id'], request['id'])
        self.assertNotEqual(operation['entity_id'], self.company_id)
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_import_guard_rejects_revoked_user_confirmation_inside_import_transaction(self):
        request = self.collect()
        result = approve_discovery_request(self.store, request['id'], 'https://employer.example/', request['revision'],
                                           confirm_company_identity=True)
        with self.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == result['operation_id'])).mappings().one())
        with self.store.engine.begin() as connection:
            connection.execute(m.company_discovery_requests.update().where(
                m.company_discovery_requests.c.id == request['id']).values(domain_evidence=[]))
        with self.store.engine.connect() as connection:
            request_source_id = connection.execute(select(m.company_discovery_requests.c.company_source_id).where(
                m.company_discovery_requests.c.id == request['id'])).scalar_one()
            source = dict(connection.execute(select(m.company_sources).where(
                m.company_sources.c.id == request_source_id)).mappings().one())
        before = self.store.count('canonical_jobs')
        with self.assertRaisesRegex(Exception, 'DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED'):
            import_company_feed_jobs(self.store, company_id=self.company_id, source_id=source['id'],
                provider=source['provider'], tenant=source['tenant'], endpoint=source['endpoint'],
                strategy='PUBLIC_JSON', items=[{'id': 'revoked-job', 'title': 'Engineer',
                    'content_text': 'Python', 'url': 'https://employer.example/jobs/revoked'}],
                import_guard=lambda connection: authorize_discovery_request(
                    self.store, operation, connection=connection))
        self.assertEqual(self.store.count('canonical_jobs'), before)

    def test_company_source_owned_by_another_employer_blocks_identity_confirmation(self):
        request = self.collect()
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {'id': 'other-employer', 'name': 'Different Employer', 'domain': ''})
        upsert_company_job_source(self.store, company_id='other-employer', provider='rss',
            tenant_identifier='other-employer', career_url='https://employer.example/careers',
            feed_url='https://employer.example/jobs.json', collection_strategy='RSS', enabled=True)
        with self.assertRaisesRegex(ValueError, 'COMPANY_IDENTITY_CONFLICT'):
            approve_discovery_request(self.store, request['id'], 'https://employer.example/careers',
                request['revision'], confirm_company_identity=True)
        self.assertEqual(self.store.count('worker_operations'), 0)
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_aggregator_host_cannot_be_confirmed_as_employer_domain(self):
        request = self.collect()
        with self.assertRaisesRegex(ValueError, 'EMPLOYER_ORIGIN_REQUIRED'):
            approve_discovery_request(self.store, request['id'], 'https://remotive.com/remote-jobs',
                request['revision'], confirm_company_identity=True)
        self.assertEqual(self.store.count('worker_operations'), 0)
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_conflicting_verified_domain_owner_blocks_confirmation_atomically(self):
        request = self.collect()
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {'id': 'other-employer', 'name': 'Other Employer', 'domain': ''})
        record_company_domain(self.store, 'other-employer', 'employer.example',
            'VERIFIED_EXISTING_DOMAIN', ['Independent owner record'])
        with self.assertRaisesRegex(ValueError, 'COMPANY_IDENTITY_CONFLICT'):
            approve_discovery_request(self.store, request['id'], 'https://employer.example/',
                request['revision'], confirm_company_identity=True)
        self.assertEqual(self.store.count('worker_operations'), 0)
        with self.store.engine.connect() as connection:
            owner_ids = set(connection.execute(select(m.company_domains.c.company_id).where(
                m.company_domains.c.domain == 'employer.example')).scalars())
        self.assertEqual(owner_ids, {'other-employer'})

    def test_disabled_source_after_confirmation_blocks_before_egress(self):
        request = self.collect()
        queued = approve_discovery_request(self.store, request['id'], 'https://employer.example/',
            request['revision'], confirm_company_identity=True)
        with self.store.engine.begin() as connection:
            connection.execute(m.job_sources.update().where(
                m.job_sources.c.id == self.source).values(enabled=False))
        with self.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == queued['operation_id'])).mappings().one())
        with self.assertRaisesRegex(Exception, 'DISCOVERY_SOURCE_DISABLED'):
            authorize_discovery_request(self.store, operation)

    def test_explicit_review_worker_imports_under_original_company_without_enabling_source(self):
        request = self.collect()
        with self.store.engine.connect() as connection:
            trigger_job_id = connection.execute(select(m.company_discovery_requests.c.trigger_job_id).where(
                m.company_discovery_requests.c.id == request['id'])).scalar_one()
            original_ats_count = len(connection.execute(select(m.company_ats_tenants.c.id).where(
                m.company_ats_tenants.c.company_id == self.company_id)).scalars().all())
        with self.store.engine.begin() as connection:
            upsert(connection, m.routes, {'id': 'employer-exact-route', 'job_id': trigger_job_id,
                'company_id': self.company_id, 'provider': 'manual', 'tenant': 'employer',
                'requisition': 'existing-role', 'url': 'https://employer.example/jobs/extra-role'})
        queued = approve_discovery_request(self.store, request['id'], 'https://employer.example/',
            request['revision'], confirm_company_identity=True)
        pages = {
            '/robots.txt': 'User-agent: *\\nAllow: /',
            '/': '<a rel="careers" href="/careers">Careers</a>',
            '/careers': '<link rel="alternate" type="application/feed+json" href="/jobs.json">',
            '/jobs.json': '{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"extra-role","title":"Python Engineer","content_text":"Python SQL","url":"https://employer.example/jobs/extra-role"}]}',
            '/jobs/extra-role': '<html>No structured evidence</html>',
        }
        def response(client, url, deadline):
            request = httpx.Request('GET', url)
            return httpx.Response(200, text=pages[request.url.path], request=request)
        with patch.dict(os.environ, self.env), patch.object(PublicCompanyClient, '_request_once', response):
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'COMPLETED', result)
        with self.store.engine.connect() as connection:
            company_ids = set(connection.execute(select(m.jobs.c.company_id)).scalars())
            sources = connection.execute(select(m.company_sources).where(
                m.company_sources.c.company_id == self.company_id,
                m.company_sources.c.provider == 'json_feed')).mappings().all()
            ats_count = connection.execute(select(m.company_ats_tenants.c.id).where(
                m.company_ats_tenants.c.company_id == self.company_id)).scalars().all()
            matching_routes = connection.execute(select(m.routes.c.job_id).where(
                m.routes.c.company_id == self.company_id,
                m.routes.c.url == 'https://employer.example/jobs/extra-role')).scalars().all()
        self.assertEqual(company_ids, {self.company_id})
        self.assertEqual(len(sources), 1)
        self.assertFalse(sources[0]['enabled'])
        self.assertIsNone(sources[0]['ats_tenant_id'])
        self.assertEqual(matching_routes, [trigger_job_id])
        self.assertEqual(len(ats_count), original_ats_count)

    def test_revocation_between_enrollment_and_import_leaves_only_disabled_candidate_source(self):
        request = self.collect()
        queued = approve_discovery_request(self.store, request['id'], 'https://employer.example/',
            request['revision'], confirm_company_identity=True)
        pages = {
            '/robots.txt': 'User-agent: *\\nAllow: /',
            '/': '<a rel="careers" href="/careers">Careers</a>',
            '/careers': '<link rel="alternate" type="application/feed+json" href="/jobs.json">',
            '/jobs.json': '{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"revoked-role","title":"Python Engineer","content_text":"Python SQL","url":"https://employer.example/jobs/revoked-role"}]}',
            '/jobs/revoked-role': '<html>No structured evidence</html>',
        }
        def response(client, url, deadline):
            request = httpx.Request('GET', url)
            return httpx.Response(200, text=pages[request.url.path], request=request)
        from services.discovery.company_feed import import_company_feed_jobs as real_import
        def revoke_before_import(store, **kwargs):
            with store.engine.begin() as connection:
                connection.execute(m.company_discovery_requests.update().where(
                    m.company_discovery_requests.c.id == request['id']).values(domain_evidence=[]))
            return real_import(store, **kwargs)
        before = self.store.count('canonical_jobs')
        with patch.dict(os.environ, self.env), \
                patch.object(PublicCompanyClient, '_request_once', response), \
                patch('services.discovery.company_feed.import_company_feed_jobs', side_effect=revoke_before_import):
            result = execute_operation(queued['operation_id'])
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(self.store.count('canonical_jobs'), before)
        with self.store.engine.connect() as connection:
            sources = connection.execute(select(m.company_sources).where(
                m.company_sources.c.company_id == self.company_id,
                m.company_sources.c.provider == 'json_feed')).mappings().all()
        self.assertEqual(len(sources), 1)
        self.assertFalse(sources[0]['enabled'])
        self.assertEqual(sources[0]['confidence'], 0.45)
        self.assertIsNone(sources[0]['ats_tenant_id'])

    def test_remotive_intake_review_and_direct_feed_keep_company_identity_and_route_provenance(self):
        from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
        aggregator_store = Store(f"sqlite:///{(self.root / 'remotive.db').as_posix()}")
        aggregator_store.migrate()
        aggregator_store.bind_workspace_mode('PERSONAL')
        aggregator_store.save_profile(copy.deepcopy(PROFILE))
        source_id = configure_source(aggregator_store, 'default', provider='remotive', poll_interval=300)
        payload = dict(RAW_REMOTIVE_PAYLOAD, jobs=[RAW_REMOTIVE_PAYLOAD['jobs'][0]])
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        try:
            collect_source(aggregator_store, source_id, 'fixture', transport=transport)
            request = list_discovery_requests(aggregator_store)[0]
            with aggregator_store.engine.connect() as connection:
                company_id = connection.execute(select(m.company_discovery_requests.c.company_id).where(
                    m.company_discovery_requests.c.id == request['id'])).scalar_one()
                trigger_job_id = connection.execute(select(m.company_discovery_requests.c.trigger_job_id).where(
                    m.company_discovery_requests.c.id == request['id'])).scalar_one()
                original_postings = len(connection.execute(select(m.postings.c.id)).scalars().all())
                original_ats = len(connection.execute(select(m.company_ats_tenants.c.id).where(
                    m.company_ats_tenants.c.company_id == company_id)).scalars().all())
            with aggregator_store.engine.begin() as connection:
                upsert(connection, m.routes, {'id': 'remotive-employer-exact-route',
                    'job_id': trigger_job_id, 'company_id': company_id, 'provider': 'manual',
                    'tenant': 'employer', 'requisition': 'existing-remotive-role',
                    'url': 'https://employer.example/jobs/extra-role'})
            queued = approve_discovery_request(aggregator_store, request['id'],
                'https://employer.example/', request['revision'], confirm_company_identity=True)
            pages = {
                '/robots.txt': 'User-agent: *\\nAllow: /',
                '/': '<a rel="careers" href="/careers">Careers</a>',
                '/careers': '<link rel="alternate" type="application/feed+json" href="/jobs.json">',
                '/jobs.json': '{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"extra-role","title":"Senior Python Engineer","content_text":"Python SQL API backend","url":"https://employer.example/jobs/extra-role"}]}',
                '/jobs/extra-role': '<html>No structured evidence</html>',
            }
            def response(client, url, deadline):
                req = httpx.Request('GET', url)
                return httpx.Response(200, text=pages[req.url.path], request=req)
            env = {'MJOBS_DATABASE_URL': aggregator_store.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
                   'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0',
                   'MJOBS_OUTPUT_DIR': str(self.root / 'remotive-outputs')}
            with patch.dict(os.environ, env), patch.object(PublicCompanyClient, '_request_once', response):
                result = execute_operation(queued['operation_id'])
            self.assertEqual(result['status'], 'COMPLETED', result)
            with aggregator_store.engine.connect() as connection:
                jobs = connection.execute(select(m.jobs)).mappings().all()
                posting_count = len(connection.execute(select(m.postings.c.id)).scalars().all())
                sources = connection.execute(select(m.company_sources).where(
                    m.company_sources.c.company_id == company_id,
                    m.company_sources.c.provider == 'json_feed')).mappings().all()
                routes = connection.execute(select(m.routes.c.job_id).where(
                    m.routes.c.company_id == company_id,
                    m.routes.c.url == 'https://employer.example/jobs/extra-role')).scalars().all()
                ats = connection.execute(select(m.company_ats_tenants.c.id).where(
                    m.company_ats_tenants.c.company_id == company_id)).scalars().all()
                domains = connection.execute(select(m.company_domains.c.id).where(
                    m.company_domains.c.company_id == company_id)).scalars().all()
            self.assertEqual({job['company_id'] for job in jobs}, {company_id})
            self.assertEqual(len(jobs), 1)
            self.assertEqual(posting_count, original_postings + 1)
            self.assertEqual(routes, [trigger_job_id])
            self.assertEqual(len(sources), 1)
            self.assertFalse(sources[0]['enabled'])
            self.assertIsNone(sources[0]['ats_tenant_id'])
            self.assertEqual(len(ats), original_ats)
            self.assertEqual(domains, [])
        finally:
            aggregator_store.engine.dispose()
