"""Real FIXTURE API/outbox/worker flow with controlled HTTP boundaries only.

No live employer, Redis or RQ acceptance is claimed. Candidate data alone is
never verified authority; all writes use the ordinary durable request path.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.requests import authorize_discovery_request, _locked_request
from services.company_discovery.public_client import PublicFetchError
from tests.integration import test_official_domain_acquisition as native
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class FixtureNativeAcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mjobs-fixture-native-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.url = getattr(self, 'database_url', 'sqlite:///' + (self.root/'proof.db').as_posix())
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        if self.store.engine.dialect.name == 'sqlite':
            self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.enterContext(patch.dict(os.environ, {'MJOBS_DATABASE_URL': self.url,
            'MJOBS_OPERATION_MODE': 'FIXTURE', 'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0',
            'MJOBS_OUTPUT_DIR': str(self.root), 'MJOBS_MOCK_ORIGIN': 'http://127.0.0.1:18799'}))
        self.client = self.enterContext(TestClient(create_app(self.store,
            'http://127.0.0.1:18799', self.root, operation_mode='FIXTURE')))
        self.assertEqual(self.client.put('/profiles', json=deepcopy(PROFILE)).status_code, 200)
        response = self.client.put('/sources', json={'provider': 'greenhouse', 'tenant': 'example',
            'poll_interval': 300, 'request_timeout': 8, 'rate_limit': 60, 'jitter_seconds': 0})
        self.assertEqual(response.status_code, 200, response.text)
        self.source_id = response.json()['id']
        self.raw = dict(RAW, absolute_url=native.JOB_URL)
        self.inventory = [self.raw]
        self.requests, self.employer_requests = [], []
        self.search_profile_id = None
        self.robots_text = 'User-agent: *\nAllow: /'
        self.redirect_target, self.after_employer_read = None, None
        self.page = ('<script type="application/ld+json">' + json.dumps({
            '@context': 'https://schema.org', '@type': 'JobPosting',
            'identifier': {'@type': 'PropertyValue', 'value': '1234'},
            'title': RAW['title'], 'url': native.JOB_URL,
            'hiringOrganization': {'@type': 'Organization', 'name': 'Example Employer',
                'url': 'https://employer.example/'}}) + '</script>'
            '<a href="https://job-boards.greenhouse.io/example/jobs/1234">Apply</a>')

    _native_boundary = native.OfficialDomainAcquisitionTests._native_boundary
    _employer_boundary = native.OfficialDomainAcquisitionTests._employer_boundary
    _collect = native.OfficialDomainAcquisitionTests._collect
    _run_company = native.OfficialDomainAcquisitionTests._run_company

    def operation(self, request):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == request['operation_id'])).mappings().one())

    def test_api_collection_acquires_only_after_reciprocal_proof_and_replays_without_egress(self):
        request = self._collect()
        operation = self.operation(request)
        self.assertEqual(operation['params']['fixture_native_acquisition'], 'native-ats-job-link-v1')
        self.assertEqual(request['authorization'], 'ATS_JOB_LINK_CANDIDATE')
        self.assertEqual(self.store.count('company_domains'), 0)
        policy_fields = ('id', 'provider', 'tenant', 'enabled', 'poll_interval', 'request_timeout',
            'rate_limit', 'jitter_seconds', 'concurrency', 'last_attempt', 'last_success')
        source_before = self.client.get('/sources').json()['sources'][0]
        before = {key: source_before[key] for key in policy_fields}
        self.assertEqual(self._run_company(request)['status'], 'COMPLETED')
        with self.store.engine.connect() as connection:
            domain = connection.execute(select(m.company_domains)).mappings().one()
            self.assertTrue(domain['verified'])
            self.assertEqual(domain['verification_method'], 'FIRST_PARTY_JOB_LINK')
            self.assertEqual(domain['company_id'], request['company_id'])
            current = connection.execute(select(m.company_discovery_requests)).mappings().one()
            self.assertEqual(current['authorization'], 'ATS_JOB_LINK_VERIFIED')
            self.assertEqual(current['profile_id'], 'fixture')
            self.assertEqual(current['candidate_id'], request['candidate_id'])
            source = connection.execute(select(m.company_sources).where(
                m.company_sources.c.career_url.is_not(None))).mappings().one()
            self.assertEqual(source['collector_source_id'], self.source_id)
        after = self.client.get('/sources').json()['sources'][0]
        self.assertEqual({key: after[key] for key in policy_fields}, before)
        self.assertEqual(self.store.count('application_attempts'), 0)
        counts = len(self.requests), len(self.employer_requests), self.store.count(m.jobs.name)
        self.store.engine.dispose()
        reopened = Store(self.url)
        self.addCleanup(reopened.engine.dispose)
        with TestClient(create_app(reopened, 'http://127.0.0.1:18799', self.root, operation_mode='FIXTURE')) as client:
            replay = client.post('/sources/' + self.source_id + '/collect', json={
                'profile_id': 'fixture', 'request_id': 'native-first-party-job'})
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertEqual(replay.json()['operation_id'], self.collection_operation_id)
        self.assertEqual(self._run_company(request)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(counts, (len(self.requests), len(self.employer_requests), self.store.count(m.jobs.name)))

    def test_forged_or_missing_marker_cannot_authorize_fixture_egress(self):
        request = self._collect()
        operation = self.operation(request)
        for changes in ({'fixture_native_acquisition': None}, {'fixture_native_acquisition': 'forged'},
                {'discovery_request_id': 'missing'}, {'discovery_request_revision': 999},
                {'profile_id': 'other'}, {'profile_version': 'stale'}, {'native_job_link': {}},
                {'fetch_mode': 'FIXTURE'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, PublicFetchError)):
                authorize_discovery_request(self.store, {**operation, 'params': {**operation['params'], **changes}})
        for changes in ({'id': 'other'}, {'entity_id': 'other'}, {'kind': 'DISCOVER'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, PublicFetchError)):
                authorize_discovery_request(self.store, {**operation, **changes})
        self.assertEqual(self.employer_requests, [])

    def test_marker_without_request_cannot_fall_back_to_unrestricted_client(self):
        request = self._collect()
        operation = self.operation(request)
        params = dict(operation['params'])
        params.pop('discovery_request_id')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation['id']).values(params=params))
        with patch('workers.tasks.PublicCompanyClient', side_effect=AssertionError('No client before authority')) as client:
            outcome = execute_operation(operation['id'])
        self.assertEqual(outcome['status'], 'BLOCKED', outcome)
        client.assert_not_called()

    def test_unmarked_public_https_operation_stays_blocked(self):
        request = self._collect()
        operation = self.operation(request)
        params = {key: value for key, value in operation['params'].items()
            if key not in {'fixture_native_acquisition', 'discovery_request_id', 'discovery_request_revision'}}
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation['id']).values(params=params))
        with patch('workers.tasks.PublicCompanyClient', side_effect=AssertionError('No generic FIXTURE public client')) as client:
            outcome = execute_operation(operation['id'])
        self.assertEqual(outcome['error_category'], 'FIXTURE_OPERATION_DISABLED', outcome)
        client.assert_not_called()

    def _assert_no_intent_after_collection(self):
        admitted = self.client.post('/sources/' + self.source_id + '/collect', json={
            'profile_id': 'fixture', 'request_id': 'no-native-proof'})
        self.assertEqual(admitted.status_code, 202, admitted.text)
        with patch('httpx.HTTPTransport.handle_request',
                lambda transport, request: self._native_boundary(transport, request)):
            outcome = execute_operation(admitted.json()['operation_id'])
        self.assertEqual(outcome['status'], 'COMPLETED', outcome)
        self.assertEqual(self.store.count('company_discovery_requests'), 0)
        self.assertEqual(self.store.count('company_domains'), 0)
        self.assertEqual(self.employer_requests, [])

    def test_shared_native_url_does_not_authorize_employer_acquisition(self):
        self.raw['absolute_url'] = 'https://job-boards.greenhouse.io/example/jobs/1234'
        self._assert_no_intent_after_collection()

    def test_disabled_profile_discovery_does_not_create_intent(self):
        profile = deepcopy(PROFILE)
        profile['preferences'] = {'company_discovery': {'company_discovery_enabled': False}}
        profile['expected_version'] = self.client.get('/profiles/fixture').json()['version']
        response = self.client.put('/profiles', json=profile)
        self.assertEqual(response.status_code, 200, response.text)
        self._assert_no_intent_after_collection()

    def test_revoked_quality_observation_prevents_client_creation(self):
        request = self._collect()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.source_quality_observations).values(succeeded=False))
        with patch('workers.tasks.PublicCompanyClient', side_effect=AssertionError('No revoked source client')) as client:
            outcome = execute_operation(request['operation_id'])
        self.assertEqual(outcome['error_category'], 'DISCOVERY_SOURCE_CHANGED', outcome)
        client.assert_not_called()

    def test_existing_personal_review_gate_is_not_widened(self):
        request = self._collect()
        with self.store.engine.begin() as connection, self.assertRaisesRegex(ValueError, 'PERSONAL_WORKSPACE_REQUIRED'):
            _locked_request(connection, request['id'], request['revision'])

    def test_other_authorization_cannot_borrow_native_marker(self):
        request = self._collect()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_discovery_requests).values(authorization='VERIFIED_DOMAIN'))
        with patch('workers.tasks.PublicCompanyClient', side_effect=AssertionError('No broad domain authority')) as client:
            outcome = execute_operation(request['operation_id'])
        self.assertEqual(outcome['error_category'], 'DISCOVERY_AUTHORIZATION_CHANGED', outcome)
        client.assert_not_called()

    test_disabled_source_revokes_egress = native.OfficialDomainAcquisitionTests.test_disabled_collector_prevents_employer_egress
    test_stale_profile_revokes_egress = native.OfficialDomainAcquisitionTests.test_changed_profile_revision_prevents_employer_egress
    test_changed_posting_revokes_egress = native.OfficialDomainAcquisitionTests.test_current_posting_link_change_revokes_immutable_snapshot_candidate
    test_cancelled_operation_no_fetch = native.OfficialDomainAcquisitionTests.test_cancelled_company_operation_does_not_fetch_candidate
    test_cross_tenant_not_verified = native.OfficialDomainAcquisitionTests.test_another_tenants_page_cannot_verify_the_employer_domain
    test_jsonld_without_reciprocal_not_verified = native.OfficialDomainAcquisitionTests.test_self_asserted_jobposting_without_reciprocal_infrastructure_stays_unverified
    test_private_dns_no_connect = native.OfficialDomainAcquisitionTests.test_candidate_dns_resolving_private_address_is_rejected_before_connect
    test_robots_denied = native.OfficialDomainAcquisitionTests.test_candidate_robots_denial_does_not_fetch_job_or_verify_domain
    test_cross_origin_redirect_blocked = native.OfficialDomainAcquisitionTests.test_cross_origin_employer_redirect_is_blocked_without_verification
    test_midresponse_revocation_no_promotion = native.OfficialDomainAcquisitionTests.test_source_revocation_during_candidate_response_prevents_promotion
    test_domain_owner_conflict_no_fetch = native.OfficialDomainAcquisitionTests.test_conflicting_domain_owner_after_admission_prevents_fetch


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'), 'disposable PostgreSQL not configured')
class FixtureNativeAcquisitionPostgresTests(FixtureNativeAcquisitionTests):
    """The same native FIXTURE oracles on an owned, migrated PostgreSQL schema."""
    def setUp(self):
        url = make_url(os.environ['MJOBS_TEST_POSTGRES_URL'])
        if (url.get_backend_name() != 'postgresql' or url.host not in {'localhost', '127.0.0.1', '::1'}
                or not url.database or not (url.database == 'mjobs_test' or url.database.startswith('mjobs_test_'))
                or url.query):
            raise ValueError('An explicit loopback disposable mjobs_test database URL is required')
        self._pg_schema = 'fixture_native_' + uuid4().hex
        self._pg_admin = create_engine(url, connect_args={'connect_timeout': 5})
        self.addCleanup(self._pg_admin.dispose)
        with self._pg_admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self._pg_schema + '"'))
        self.addCleanup(self._drop_owned_schema)
        self.database_url = url.update_query_dict({'options': '-csearch_path=' + self._pg_schema
            + ' -clock_timeout=5000 -cstatement_timeout=30000'}).render_as_string(hide_password=False)
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL': self.database_url}):
            command.upgrade(Config('alembic.ini'), 'head')
        super().setUp()

    def _drop_owned_schema(self):
        suffix = self._pg_schema.removeprefix('fixture_native_')
        if not self._pg_schema.startswith('fixture_native_') or len(suffix) != 32 or any(
                char not in '0123456789abcdef' for char in suffix):
            raise ValueError('Unexpected disposable schema identifier')
        with self._pg_admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self._pg_schema + '" CASCADE'))
