"""MJ-REQ-060: native ATS job links are candidates until independently corroborated.

Public HTTP producers and real operation handlers use a private synthetic store.
Only external HTTP responses are controlled; this is not Redis/RQ or live-provider
evidence. No verified domain is seeded and no application endpoint is called.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.public_client import PublicCompanyClient
from services.discovery.scheduler import schedule_due_sources, source_due_at
from tests.fixtures.company_dns import owned_resolvers
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


JOB_URL = 'https://employer.example/careers/python?gh_jid=1234'


class OfficialDomainAcquisitionTests(unittest.TestCase):
    def _prepare_schema(self):
        # PostgreSQL wrappers apply versioned Alembic migrations to their owned
        # schema before shared setup. Store.migrate is intentionally SQLite-only.
        if self.store.url.startswith('sqlite:'):
            self.store.migrate()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mjobs-domain-acquisition-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.url = getattr(self, 'database_url', 'sqlite:///' + (self.root / 'proof.db').as_posix())
        self.store = Store(self.url)
        self.addCleanup(lambda: self.store.engine.dispose())
        self._prepare_schema()
        self.store.bind_workspace_mode('PERSONAL')
        self.enterContext(patch.dict(os.environ, {
            'MJOBS_DATABASE_URL': self.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
            'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0', 'MJOBS_OUTPUT_DIR': str(self.root),
        }))
        self.client = self.enterContext(TestClient(create_app(
            self.store, '', self.root, operation_mode='PERSONAL')))
        self.assertEqual(self.client.put('/profiles', json=deepcopy(PROFILE)).status_code, 200)
        response = self.client.put('/sources', json={
            'provider': 'greenhouse', 'tenant': 'example', 'poll_interval': 300,
            'request_timeout': 8, 'rate_limit': 60, 'jitter_seconds': 0,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.source_id = response.json()['id']
        self.raw = dict(RAW, absolute_url=JOB_URL)
        self.inventory = [self.raw]
        self.requests = []
        self.employer_requests = []
        self.search_profile_id = None
        self.robots_text = 'User-agent: *\nAllow: /'
        self.redirect_target = None
        self.after_employer_read = None
        self.page = ('<script type="application/ld+json">' + json.dumps({
            '@context': 'https://schema.org', '@type': 'JobPosting',
            'identifier': {'@type': 'PropertyValue', 'value': '1234'},
            'title': RAW['title'], 'url': JOB_URL,
            'hiringOrganization': {'@type': 'Organization', 'name': 'Example Employer',
                                   'url': 'https://employer.example/'},
        }) + '</script><a href="https://job-boards.greenhouse.io/example/jobs/1234">Apply</a>')

    def _native_boundary(self, _transport, request):
        self.assertEqual(request.method, 'GET')
        self.assertEqual(request.url.host, 'boards-api.greenhouse.io')
        self.requests.append(str(request.url))
        if request.url.path == '/v1/boards/example/jobs':
            return httpx.Response(200, json={'jobs': self.inventory})
        self.assertEqual(request.url.path, '/v1/boards/example')
        return httpx.Response(200, json={'name': 'Example Employer'})

    def _collect(self):
        payload = {'profile_id': 'fixture', 'request_id': 'native-first-party-job'}
        if self.search_profile_id:
            payload['search_profile_id'] = self.search_profile_id
        admitted = self.client.post('/sources/' + self.source_id + '/collect',
            json=payload)
        self.assertEqual(admitted.status_code, 202, admitted.text)
        self.collection_operation_id = admitted.json()['operation_id']
        with patch('httpx.HTTPTransport.handle_request',
                   lambda transport, request: self._native_boundary(transport, request)):
            outcome = execute_operation(admitted.json()['operation_id'])
        self.assertEqual(outcome['status'], 'COMPLETED', outcome)
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.company_discovery_requests)).mappings().one())

    def test_native_first_party_job_link_queues_corroboration_without_domain_authority(self):
        request = self._collect()
        self.assertEqual(request['state'], 'QUEUED')
        self.assertEqual(request['selected_website'], JOB_URL)
        self.assertEqual(request['authorization'], 'ATS_JOB_LINK_CANDIDATE')
        self.assertIsNotNone(request['operation_id'])
        self.assertEqual(self.store.count('company_domains'), 0)
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == request['operation_id'])).mappings().one()
            self.assertEqual(operation['kind'], 'COMPANY_DISCOVER')
            self.assertEqual(operation['entity_id'], request['company_id'])
            self.assertEqual(operation['params']['profile_version'], self.store.profile('fixture')['version'])
            self.assertEqual(operation['state'], 'QUEUED')
        self.assertEqual(len(self.requests), 2)

    def _employer_boundary(self, _client, url, deadline):
        self.employer_requests.append(url)
        request = httpx.Request('GET', url)
        self.assertEqual(request.url.host, 'employer.example')
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text=self.robots_text, request=request)
        self.assertEqual(url, JOB_URL)
        if self.after_employer_read:
            self.after_employer_read()
        if self.redirect_target:
            return httpx.Response(302, headers={'location': self.redirect_target}, request=request)
        return httpx.Response(200, text=self.page, request=request)

    def _run_company(self, request):
        with patch('httpx.HTTPTransport.handle_request',
                   lambda transport, req: self._native_boundary(transport, req)), patch.object(
                       PublicCompanyClient, '_request_once',
                       lambda client, url, deadline: self._employer_boundary(client, url, deadline)):
            return execute_operation(request['operation_id'])

    def test_reciprocal_native_job_page_enrolls_and_survives_reopen_and_replay(self):
        response = self.client.put('/search-profiles', json={'id': 'native-schedule',
            'profile_id': 'fixture', 'name': 'Synthetic native schedule',
            'source_ids': [self.source_id], 'enabled': True})
        self.assertEqual(response.status_code, 200, response.text)
        request = self._collect()
        self.assertIsNotNone(request['operation_id'])
        response = self.client.put('/company-sources/' + request['company_source_id'] + '/pin',
            json={'pinned': True})
        self.assertEqual(response.status_code, 200, response.text)
        with self.store.engine.connect() as connection:
            before_policy = dict(connection.execute(select(m.job_sources)).mappings().one())
        self.inventory.append(dict(RAW, id=1235, internal_job_id=5679, title='Python Data Engineer',
            absolute_url='https://job-boards.greenhouse.io/example/jobs/1235'))
        outcome = self._run_company(request)
        self.assertEqual(outcome['status'], 'COMPLETED', outcome)
        with self.store.engine.connect() as connection:
            domain = connection.execute(select(m.company_domains)).mappings().one()
            self.assertTrue(domain['verified'])
            self.assertEqual(domain['verification_method'], 'FIRST_PARTY_JOB_LINK')
            self.assertEqual(domain['company_id'], request['company_id'])
            self.assertIn('snapshot_sha256', ' '.join(domain['evidence']))
            source = connection.execute(select(m.company_sources).where(
                m.company_sources.c.career_url.is_not(None))).mappings().one()
            self.assertTrue(source['enabled'])
            self.assertTrue(source['discovered_automatically'])
            self.assertEqual(source['collector_source_id'], self.source_id)
            original_source = connection.execute(select(m.company_sources).where(
                m.company_sources.c.id == request['company_source_id'])).mappings().one()
            self.assertEqual(original_source['priority'], 'USER_PINNED')
            self.assertEqual(original_source['origin'], 'USER_WATCHED')
            deferred = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.action == 'COMPANY_ATS_INVENTORY_DEFERRED')).scalar_one()
            self.assertEqual(deferred['inventory_status'], 'RETAINED_OBSERVATIONS')
            self.assertEqual(deferred['inventory_scope'], 'OBSERVED_NATIVE_POSTINGS')
            self.assertEqual(deferred['jobs_imported'], 0)
            self.assertEqual(deferred['jobs_retained'], 1)
            self.assertEqual(deferred['refresh_dispatch'], 'EXISTING_SOURCE_SCHEDULER')
            self.assertIsNotNone(deferred['inventory_receipt']['operation_id'])
            collector = connection.execute(select(m.job_sources)).mappings().one()
            for key in ('poll_interval', 'request_timeout', 'rate_limit', 'jitter_seconds',
                        'concurrency', 'enabled', 'last_attempt', 'last_success'):
                self.assertEqual(collector[key], before_policy[key], key)
            due = source_due_at(collector) + timedelta(seconds=1)
            jobs = list(connection.execute(select(m.jobs)).mappings())
            self.assertEqual(len(jobs), 1)
        self.assertEqual(len(self.requests), 2, 'Acquisition must respect the existing native poll cooldown')
        scheduled = schedule_due_sources(self.store, at=due)
        self.assertEqual(len(scheduled['scheduled']), 1, scheduled)
        class CollectionClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return due.astimezone(tz) if tz else due.replace(tzinfo=None)
        with patch('httpx.HTTPTransport.handle_request',
                   lambda transport, req: self._native_boundary(transport, req)), patch(
                       'services.discovery.sources.collect.datetime', CollectionClock), patch(
                           'services.discovery.sources.collect.now', return_value=due.isoformat()):
            refreshed = execute_operation(scheduled['scheduled'][0]['operation_id'])
        self.assertEqual(refreshed['status'], 'COMPLETED', refreshed)
        with self.store.engine.connect() as connection:
            jobs = list(connection.execute(select(m.jobs)).mappings())
            self.assertEqual(len(jobs), 2)
            self.assertEqual({job['company_id'] for job in jobs}, {request['company_id']})
            extra = next(job for job in jobs if job['requisition'] == '1235')
            self.assertEqual(extra['attributes']['source_resolution']['authority'], 'VERIFIED_ATS')
        counts = len(self.requests), len(self.employer_requests)
        self.store.engine.dispose()
        reopened = Store(self.url)
        self.addCleanup(reopened.engine.dispose)
        with TestClient(create_app(reopened, '', self.root, operation_mode='PERSONAL')) as client:
            response = client.get('/jobs', params={'profile_id': 'fixture'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()['jobs']), 2)
            detail = client.get('/jobs/' + extra['id'], params={'profile_id': 'fixture'})
            self.assertEqual(detail.status_code, 200, detail.text)
            value = detail.json()
            self.assertEqual(value['company_id'], request['company_id'])
            self.assertEqual(value['application_url'], self.inventory[1]['absolute_url'])
            self.assertEqual(value['provider'], 'greenhouse')
            self.assertEqual(len(value['postings']), 1)
            self.assertEqual(value['attributes']['source_resolution']['authority'], 'VERIFIED_ATS')
            self.assertEqual({route['provider'] for route in value['routes']}, {'greenhouse'})
            self.assertEqual(reopened.count('application_attempts'), 0)
            replay = client.post('/sources/' + self.source_id + '/collect',
                json={'profile_id': 'fixture', 'request_id': 'native-first-party-job'})
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertEqual(replay.json()['operation_id'], self.collection_operation_id)
        self.assertEqual(self._run_company(request)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(counts, (len(self.requests), len(self.employer_requests)))

    def test_another_tenants_page_cannot_verify_the_employer_domain(self):
        request = self._collect()
        self.page = self.page.replace('/example/jobs/1234', '/other/jobs/1234')
        outcome = self._run_company(request)
        self.assertEqual(outcome['status'], 'BLOCKED', outcome)
        self.assertEqual(outcome['error_category'], 'COMPANY_DOMAIN_CORROBORATION_REQUIRED')
        self.assertEqual(self.store.count('company_domains'), 0)
        self.assertEqual(len(self.requests), 2)

    def test_self_asserted_jobposting_without_reciprocal_infrastructure_stays_unverified(self):
        request = self._collect()
        self.page = self.page.split('</script>')[0] + '</script>'
        outcome = self._run_company(request)
        self.assertEqual(outcome['error_category'], 'COMPANY_DOMAIN_CORROBORATION_REQUIRED', outcome)
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_disabled_collector_prevents_employer_egress(self):
        request = self._collect()
        response = self.client.put('/sources', json={
            'provider': 'greenhouse', 'tenant': 'example', 'enabled': False})
        self.assertEqual(response.status_code, 200, response.text)
        outcome = self._run_company(request)
        self.assertEqual(outcome['error_category'], 'DISCOVERY_SOURCE_DISABLED', outcome)
        self.assertEqual(self.employer_requests, [])
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_changed_profile_revision_prevents_employer_egress(self):
        request = self._collect()
        profile = deepcopy(PROFILE)
        profile['name'] = 'Changed synthetic profile'
        profile['expected_version'] = self.client.get('/profiles/fixture').json()['version']
        response = self.client.put('/profiles', json=profile)
        self.assertEqual(response.status_code, 200, response.text)
        outcome = self._run_company(request)
        self.assertEqual(outcome['error_category'], 'DISCOVERY_SOURCE_CHANGED', outcome)
        self.assertEqual(self.employer_requests, [])

    def test_current_posting_link_change_revokes_immutable_snapshot_candidate(self):
        request = self._collect()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.postings).values(raw=dict(self.raw,
                absolute_url='https://another.example/jobs/1234')))
        outcome = self._run_company(request)
        self.assertEqual(outcome['error_category'], 'DISCOVERY_SOURCE_CHANGED', outcome)
        self.assertEqual(self.employer_requests, [])

    def test_cancelled_company_operation_does_not_fetch_candidate(self):
        request = self._collect()
        response = self.client.post('/operations/' + request['operation_id'] + '/cancel')
        self.assertEqual(response.status_code, 202, response.text)
        outcome = self._run_company(request)
        self.assertEqual(outcome['status'], 'ALREADY_CLAIMED_OR_CAPACITY', outcome)
        self.assertEqual(self.client.get('/operations/' + request['operation_id']).json()['state'], 'CANCELLED')
        self.assertEqual(self.employer_requests, [])

    def test_candidate_dns_resolving_private_address_is_rejected_before_connect(self):
        request = self._collect()
        private_answers = [(
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443))]
        # DNS now runs in an isolated, request-owned child. Inject the child's
        # synthetic answer while retaining the parent-side no-connect guard.
        with owned_resolvers(answers=private_answers) as resolver, patch(
                'socket.socket', side_effect=AssertionError(
                    'Private candidate must never connect')) as sockets:
            outcome = execute_operation(request['operation_id'])
        self.assertEqual(outcome['error_category'], 'ROBOTS_UNAVAILABLE', outcome)
        self.assertEqual(len(resolver.processes), 1, 'Expected one owned DNS lookup')
        resolver.assert_reaped()
        sockets.assert_not_called()
        self.assertEqual(self.store.count('company_domains'), 0)

    def _deferred_inventory(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(m.audit.c.payload).where(
                m.audit.c.action == 'COMPANY_ATS_INVENTORY_DEFERRED')).scalar_one()

    def _assert_deferred_without_cached_count(self, request):
        outcome = self._run_company(request)
        self.assertEqual(outcome['status'], 'COMPLETED', outcome)
        inventory = self._deferred_inventory()
        self.assertEqual(inventory['inventory_status'], 'DEFERRED')
        self.assertEqual(inventory['inventory_scope'], 'NOT_ESTABLISHED')
        self.assertIsNone(inventory['jobs_retained'])
        self.assertIsNone(inventory['jobs_enumerated'])
        self.assertEqual(inventory['jobs_imported'], 0)
        self.assertEqual(len(self.requests), 2)
        return inventory

    def test_missing_completed_collection_receipt_defers_without_inventing_inventory(self):
        request = self._collect()
        # Controlled damaged/legacy receipt: persisted postings are insufficient
        # to claim a named successful public operation still exists.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.collection_operation_id).values(state='BLOCKED'))
        inventory = self._assert_deferred_without_cached_count(request)
        self.assertEqual(inventory['refresh_dispatch'], 'SEARCH_PROFILE_REQUIRED')

    def test_partial_collection_receipt_is_not_reused_as_inventory(self):
        request = self._collect()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.source_quality_observations).values(enumerated=2))
        self._assert_deferred_without_cached_count(request)

    def test_filtered_public_collection_receipt_is_not_reused_as_unfiltered_inventory(self):
        response = self.client.put('/search-profiles', json={'id': 'filtered-native',
            'profile_id': 'fixture', 'name': 'Synthetic filtered search',
            'source_ids': [self.source_id], 'enabled': True, 'required_keywords': ['Python']})
        self.assertEqual(response.status_code, 200, response.text)
        self.search_profile_id = 'filtered-native'
        request = self._collect()
        self._assert_deferred_without_cached_count(request)

    def test_old_receipt_inside_a_long_poll_cadence_is_not_labeled_fresh(self):
        response = self.client.put('/sources', json={
            'provider': 'greenhouse', 'tenant': 'example', 'poll_interval': 3600,
            'request_timeout': 8, 'rate_limit': 60, 'jitter_seconds': 0})
        self.assertEqual(response.status_code, 200, response.text)
        request = self._collect()
        later = datetime.now(timezone.utc) + timedelta(seconds=901)
        class ReceiptClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return later.astimezone(tz) if tz else later.replace(tzinfo=None)
        with patch('services.company_discovery.ats_enrollment.datetime', ReceiptClock):
            self._assert_deferred_without_cached_count(request)

    def test_aggregator_link_is_not_admitted_as_employer_origin(self):
        self.raw['absolute_url'] = 'https://www.linkedin.com/jobs/view/1234'
        request = self._collect()
        self.assertEqual(request['state'], 'NEEDS_ORIGIN_REVIEW')
        self.assertIsNone(request['operation_id'])
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_literal_private_address_is_not_admitted_as_employer_origin(self):
        self.raw['absolute_url'] = 'https://127.0.0.1/careers/1234'
        request = self._collect()
        self.assertEqual(request['state'], 'NEEDS_ORIGIN_REVIEW')
        self.assertIsNone(request['operation_id'])

    def test_conflicting_domain_owner_after_admission_prevents_fetch(self):
        request = self._collect()
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(id='unrelated-owner', name='Unrelated Employer', domain=''))
            connection.execute(m.company_domains.insert().values(id='conflicting-owner-proof',
                company_id='unrelated-owner', domain='employer.example', verified=True, confidence=.99,
                verification_method='FIRST_PARTY_JOB_LINK', first_seen_at=datetime.now(timezone.utc).isoformat(),
                last_verified_at=datetime.now(timezone.utc).isoformat(), evidence=['Synthetic competing owner']))
        outcome = self._run_company(request)
        self.assertEqual(outcome['error_category'], 'DISCOVERY_SOURCE_CHANGED', outcome)
        self.assertEqual(self.employer_requests, [])
        self.assertEqual(self.store.count('company_domains'), 1)

    def test_candidate_robots_denial_does_not_fetch_job_or_verify_domain(self):
        request = self._collect()
        self.robots_text = 'User-agent: *\nDisallow: /'
        outcome = self._run_company(request)
        self.assertEqual(outcome['error_category'], 'ROBOTS_DENIED', outcome)
        self.assertEqual(self.employer_requests, ['https://employer.example/robots.txt'])
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_cross_origin_employer_redirect_is_blocked_without_verification(self):
        request = self._collect()
        self.redirect_target = 'https://another.example/careers/python'
        outcome = self._run_company(request)
        self.assertEqual(outcome['error_category'], 'REDIRECT_BLOCKED', outcome)
        self.assertEqual(self.store.count('company_domains'), 0)
        self.assertEqual(len(self.employer_requests), 2)

    def test_source_revocation_during_candidate_response_prevents_promotion(self):
        request = self._collect()
        def revoke():
            response = self.client.put('/sources', json={
                'provider': 'greenhouse', 'tenant': 'example', 'enabled': False})
            self.assertEqual(response.status_code, 200, response.text)
        self.after_employer_read = revoke
        outcome = self._run_company(request)
        self.assertEqual(outcome['error_category'], 'DISCOVERY_SOURCE_DISABLED', outcome)
        self.assertEqual(self.store.count('company_domains'), 0)

    def test_revoked_acquired_domain_blocks_subsequent_native_refresh_before_egress(self):
        request = self._collect()
        self.assertEqual(self._run_company(request)['status'], 'COMPLETED')
        calls = len(self.requests), len(self.employer_requests)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_domains).values(verified=False))
        admitted = self.client.post('/sources/' + self.source_id + '/collect',
            json={'profile_id': 'fixture', 'request_id': 'revoked-domain-refresh'})
        self.assertEqual(admitted.status_code, 202, admitted.text)
        with patch('httpx.HTTPTransport.handle_request',
                   lambda transport, req: self._native_boundary(transport, req)):
            outcome = execute_operation(admitted.json()['operation_id'])
        self.assertEqual(outcome['error_category'], 'COMPANY_DOMAIN_REVIEW_REQUIRED', outcome)
        self.assertEqual(calls, (len(self.requests), len(self.employer_requests)))

    def test_multiple_existing_verified_domains_require_review(self):
        request = self._collect()
        with self.store.engine.begin() as connection:
            connection.execute(m.company_domains.insert().values(id='ambiguous-other-domain',
                company_id=request['company_id'], domain='another-employer.example', verified=True, confidence=.99,
                verification_method='FIRST_PARTY_JOB_LINK', first_seen_at=datetime.now(timezone.utc).isoformat(),
                last_verified_at=datetime.now(timezone.utc).isoformat(), evidence=['Synthetic competing domain']))
        outcome = self._run_company(request)
        self.assertEqual(outcome['error_category'], 'DISCOVERY_SOURCE_CHANGED', outcome)
        self.assertEqual(self.employer_requests, [])
