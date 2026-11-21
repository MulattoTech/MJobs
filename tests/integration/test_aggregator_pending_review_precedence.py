"""Explicit employer review safely supersedes only an unclaimed bootstrap hint."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import httpx
import unittest
from unittest.mock import patch
import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from packages.persistence import models as m
from packages.persistence.store import upsert
from services.company_discovery import requests
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.requests import approve_discovery_request
from services.discovery.sources.collect import collect_source
from services.discovery.sources.greenhouse import Greenhouse
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from workers.operations import claim
from workers.dispatch import enqueue_operation
from workers.tasks import execute_operation


class AggregatorPendingReviewPrecedenceTests(unittest.TestCase):
    def setUp(self):
        from tests.integration.test_reviewed_greenhouse_enrollment import ReviewedGreenhouseEnrollmentTests

        self.case = ReviewedGreenhouseEnrollmentTests()
        if hasattr(self, 'database_url'):
            self.case.database_url = self.database_url
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.case.tearDown)
        self.request = self._stage_greenhouse_request(self.case, consent='enabled')
        with self.case.store.engine.connect() as connection:
            self.operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.request['operation_id'])).mappings().one())
        self.assertEqual(self.request['authorization'], 'AGGREGATOR_BOOTSTRAP_PENDING')
        self.assertEqual(self.operation['state'], 'QUEUED')

    def _stage_greenhouse_request(self, case, *, consent):
        profile = case.store.profile('fixture')
        preferences = deepcopy(profile['preferences'])
        company_policy = preferences.setdefault('company_discovery', {})
        company_policy['company_discovery_enabled'] = True
        if consent == 'enabled':
            company_policy['employer_verification'] = {
                'version': 1, 'mode': 'GREENHOUSE_RECIPROCAL_V1', 'activation_id': None}
        elif consent == 'disabled':
            company_policy['employer_verification'] = {
                'version': 1, 'mode': 'DISABLED', 'activation_id': None}
        elif consent == 'absent':
            company_policy.pop('employer_verification', None)
        profile['preferences'] = preferences
        case.store.save_profile(profile, expected_version=profile['version'])
        with case.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == case.source).values(rate_limit=1))

        current = case.store.profile('fixture')
        parent_request_id = 'manual-review-parent-' + uuid4().hex
        parent_id = enqueue_operation(case.store, 'DISCOVER', 'fixture', {
            'source_id': case.source, 'request_id': parent_request_id,
            'profile_version': current['version'],
        })
        parent = claim(case.store, parent_id)
        self.assertIsNotNone(parent)
        payload = deepcopy(RAW_REMOTIVE_PAYLOAD)
        payload['jobs'][0]['description'] += (
            '<a href="https://job-boards.greenhouse.io/reviewed/jobs/101">Apply</a>')
        case.review_bootstrap_payload = payload
        case.review_bootstrap_lineage = {
            'parent_operation_id': parent_id,
            'parent_operation_attempt': parent['attempt_count'],
        }
        collect_source(case.store, case.source, 'fixture',
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
            **case.review_bootstrap_lineage)
        with case.store.engine.connect() as connection:
            row = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.profile_id == 'fixture').order_by(
                    m.company_discovery_requests.c.created_at.desc())).mappings().first()
        if row is None:
            return None
        request = dict(row)
        with case.store.engine.begin() as connection:
            upsert(connection, m.routes, {'id': 'reviewed-employer-original-route',
                'job_id': request['trigger_job_id'], 'company_id': request['company_id'],
                'provider': 'greenhouse', 'tenant': 'reviewed', 'requisition': '101',
                'url': 'https://job-boards.greenhouse.io/reviewed/jobs/101'})
        return request

    def _new_manual_review_case(self, consent):
        from tests.integration.test_reviewed_greenhouse_enrollment import ReviewedGreenhouseEnrollmentTests

        case = ReviewedGreenhouseEnrollmentTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        request = self._stage_greenhouse_request(case, consent=consent)
        return case, request

    def _assert_parked_without_automatic_authority(self, case, request):
        self.assertIsNotNone(request, 'aggregator candidate should remain available for manual review')
        self.assertEqual(request['state'], 'NEEDS_ORIGIN_REVIEW')
        self.assertIsNone(request['selected_website'])
        self.assertIsNone(request['authorization'])
        self.assertIsNone(request['operation_id'])
        self.assertEqual(request['domain_evidence'], [])
        self.assertEqual(case.fetches, [])
        self.assertEqual(case.store.count('company_domains'), 0)
        with case.store.engine.connect() as connection:
            company_operations = connection.execute(select(m.operations.c.id).where(
                m.operations.c.kind == 'COMPANY_DISCOVER')).all()
            enrolled = connection.execute(select(m.company_sources.c.id).where(
                m.company_sources.c.tenant == 'reviewed')).all()
        self.assertEqual(company_operations, [])
        self.assertEqual(enrolled, [])

    def test_disabled_consent_parks_review_and_explicit_confirmation_enrolls(self):
        case, request = self._new_manual_review_case('disabled')
        self._assert_parked_without_automatic_authority(case, request)

        receipt = approve_discovery_request(case.store, request['id'],
            'https://employer.example/', request['revision'], confirm_company_identity=True)
        with patch.dict(os.environ, case.env), \
                patch.object(PublicCompanyClient, '_request_once',
                    lambda client, url, deadline: case.page(client, url, deadline)), \
                patch.object(Greenhouse, '_get',
                    lambda adapter, suffix, params=None: case.board(adapter, suffix, params)):
            result = execute_operation(receipt['operation_id'])

        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertTrue(case.fetches)
        with case.store.engine.connect() as connection:
            reviewed_source = connection.execute(select(m.company_sources).where(
                m.company_sources.c.tenant == 'reviewed')).mappings().one()
            current = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request['id'])).mappings().one()
        self.assertTrue(reviewed_source['enabled'])
        self.assertEqual(current['authorization'], 'USER_CONFIRMED_DOMAIN')

    def test_parked_no_consent_review_does_not_auto_queue_existing_verified_origin(self):
        from services.company_discovery.domains import record_company_domain

        case, request = self._new_manual_review_case('disabled')
        self._assert_parked_without_automatic_authority(case, request)
        record_company_domain(case.store, request['company_id'], 'employer.example',
            'VERIFIED_EXISTING_DOMAIN', ['Synthetic reviewed evidence'])
        with case.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == case.source).values(last_attempt=None))
        collect_source(case.store, case.source, 'fixture',
            transport=httpx.MockTransport(lambda http_request: httpx.Response(
                200, json=case.review_bootstrap_payload)),
            **case.review_bootstrap_lineage)

        with case.store.engine.connect() as connection:
            current = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request['id'])).mappings().one())
            children = connection.execute(select(m.operations.c.id).where(
                m.operations.c.kind == 'COMPANY_DISCOVER')).all()
        self.assertEqual(current['state'], 'NEEDS_ORIGIN_REVIEW')
        self.assertIsNone(current['selected_website'])
        self.assertIsNone(current['authorization'])
        self.assertIsNone(current['operation_id'])
        self.assertEqual(current['domain_evidence'], [])
        self.assertEqual(children, [])
        self.assertEqual(case.fetches, [])

    def test_absent_consent_parks_review_and_explicit_candidate_stays_unverified(self):
        case, request = self._new_manual_review_case('absent')
        self._assert_parked_without_automatic_authority(case, request)

        receipt = approve_discovery_request(case.store, request['id'],
            'https://employer.example/', request['revision'], confirm_company_identity=False)
        with case.store.engine.connect() as connection:
            reviewed = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request['id'])).mappings().one()
        self.assertEqual(reviewed['authorization'], 'USER_CANDIDATE')
        with patch.dict(os.environ, case.env), \
                patch.object(PublicCompanyClient, '_request_once',
                    lambda client, url, deadline: case.page(client, url, deadline)), \
                patch.object(Greenhouse, '_get',
                    side_effect=AssertionError('Unconfirmed candidate must not enumerate')):
            result = execute_operation(receipt['operation_id'])

        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(result['error_category'], 'COMPANY_DOMAIN_REVIEW_REQUIRED')
        with case.store.engine.connect() as connection:
            enrolled = connection.execute(select(m.company_sources.c.id).where(
                m.company_sources.c.tenant == 'reviewed')).all()
            current = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request['id'])).mappings().one()
        self.assertEqual(enrolled, [])
        self.assertEqual(current['authorization'], 'USER_CANDIDATE')
        self.assertEqual(case.store.count('company_domains'), 0)

    def test_user_review_supersedes_only_queued_typed_bootstrap(self):
        receipt = approve_discovery_request(self.case.store, self.request['id'],
            'https://employer.example/', self.request['revision'], confirm_company_identity=True)
        with self.case.store.engine.connect() as connection:
            old_operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.operation['id'])).mappings().one())
            request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request['id'])).mappings().one())
            new_operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == receipt['operation_id'])).mappings().one())
            audit = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.request['id'],
                m.audit.c.action == 'COMPANY_DISCOVERY_BOOTSTRAP_SUPERSEDED')).mappings().one_or_none()

        self.assertNotEqual(receipt['operation_id'], self.operation['id'])
        self.assertEqual((old_operation['state'], old_operation['cancel_requested']), ('CANCELLED', True))
        self.assertEqual(old_operation['params'], self.operation['params'])
        self.assertEqual((request['authorization'], request['state']), ('USER_CONFIRMED_DOMAIN', 'QUEUED'))
        self.assertEqual(request['revision'], self.request['revision'] + 1)
        self.assertEqual(request['operation_id'], receipt['operation_id'])
        self.assertEqual(request['domain_evidence'][0]['method'], 'USER_CONFIRMED_DOMAIN')
        self.assertNotIn('AGGREGATOR_BOOTSTRAP_PENDING_V1', str(request['domain_evidence']))
        self.assertEqual(new_operation['params']['discovery_request_id'], self.request['id'])
        self.assertNotIn('aggregator_bootstrap', new_operation['params'])
        self.assertIsNotNone(audit)
        self.assertEqual(audit['payload']['previous_operation_id'], self.operation['id'])
        self.assertEqual(audit['payload']['previous_pending_sha256'], self.request['domain_evidence'][0]['sha256'])

    def test_claimed_cancelled_or_expired_bootstrap_cannot_be_superseded(self):
        cases = (
            {'state': 'RUNNING', 'cancel_requested': False,
             'lease_expires_at': (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()},
            {'state': 'RUNNING', 'cancel_requested': True,
             'lease_expires_at': (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()},
            {'state': 'RUNNING', 'cancel_requested': False,
             'lease_expires_at': '2000-01-01T00:00:00+00:00'},
        )
        for index, values in enumerate(cases):
            with self.subTest(values=values):
                if index:
                    with self.case.store.engine.begin() as connection:
                        connection.execute(update(m.operations).where(
                            m.operations.c.id == self.operation['id']).values(
                                state='QUEUED', cancel_requested=False,
                                lease_expires_at=None))
                with self.case.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(
                        m.operations.c.id == self.operation['id']).values(**values))
                with self.assertRaisesRegex(ValueError, 'DISCOVERY_REQUEST_NOT_REVIEWABLE'):
                    approve_discovery_request(self.case.store, self.request['id'],
                        'https://employer.example/', self.request['revision'], confirm_company_identity=True)
                with self.case.store.engine.connect() as connection:
                    operation = connection.execute(select(m.operations).where(
                        m.operations.c.id == self.operation['id'])).mappings().one()
                    request = connection.execute(select(m.company_discovery_requests).where(
                        m.company_discovery_requests.c.id == self.request['id'])).mappings().one()
                self.assertEqual(operation['state'], 'RUNNING')
                self.assertEqual(request['operation_id'], self.operation['id'])
                self.assertEqual(request['authorization'], 'AGGREGATOR_BOOTSTRAP_PENDING')

    def test_stale_pending_operation_reference_cannot_be_superseded(self):
        with self.case.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation['id']).values(params={
                    **self.operation['params'],
                    'aggregator_bootstrap_pending_sha256': '0' * 64,
                }))
        with self.assertRaisesRegex(ValueError, 'DISCOVERY_REQUEST_NOT_REVIEWABLE'):
            approve_discovery_request(self.case.store, self.request['id'],
                'https://employer.example/', self.request['revision'], confirm_company_identity=True)
        with self.case.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == self.operation['id'])).mappings().one()
            request = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request['id'])).mappings().one()
        self.assertEqual(operation['state'], 'QUEUED')
        self.assertEqual(request['operation_id'], self.operation['id'])
        self.assertEqual(request['authorization'], 'AGGREGATOR_BOOTSTRAP_PENDING')

    def test_claim_racing_the_supersession_cannot_be_cancelled(self):
        current_bootstrap = requests._current_aggregator_bootstrap

        def claim_after_validation(connection, operation, request, *, fixture):
            current = current_bootstrap(connection, operation, request, fixture=fixture)
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation['id']).values(
                    state='RUNNING', lease_expires_at=(
                        datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()))
            return current

        with patch.object(requests, '_current_aggregator_bootstrap', claim_after_validation):
            with self.assertRaisesRegex(ValueError, 'DISCOVERY_REQUEST_NOT_REVIEWABLE'):
                approve_discovery_request(self.case.store, self.request['id'],
                    'https://employer.example/', self.request['revision'], confirm_company_identity=True)

        with self.case.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == self.operation['id'])).mappings().one()
            request = connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == self.request['id'])).mappings().one()
        self.assertEqual(operation['state'], 'QUEUED')
        self.assertFalse(operation['cancel_requested'])
        self.assertEqual(request['operation_id'], self.operation['id'])
        self.assertEqual(request['authorization'], 'AGGREGATOR_BOOTSTRAP_PENDING')


POSTGRES_URL = os.environ.get('MJOBS_TEST_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'NOT_RUN: disposable loopback MJOBS_TEST_POSTGRES_URL absent')
class AggregatorPendingReviewPostgresTests(AggregatorPendingReviewPrecedenceTests):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith('postgresql') or url.host not in ('localhost', '127.0.0.1', '::1')
                or url.query or not url.database or not url.database.startswith('mjobs_test')):
            raise RuntimeError('Disposable loopback mjobs_test PostgreSQL without URL options required')
        self.schema = 'aggregator_pending_review_' + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.database_url = url.update_query_dict({'options': '-csearch_path=' + self.schema +
            ' -clock_timeout=5000 -cstatement_timeout=30000'}).render_as_string(hide_password=False)
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL': self.database_url}):
            command.upgrade(Config('alembic.ini'), 'head')
        super().setUp()
        self.assertEqual(self.case.store.engine.dialect.name, 'postgresql')

    def cleanup_schema(self):
        prefix = 'aggregator_pending_review_'
        suffix = self.schema.removeprefix(prefix)
        if not self.schema.startswith(prefix) or len(suffix) != 32 or any(
                character not in '0123456789abcdef' for character in suffix):
            raise RuntimeError('Refusing to drop an unowned PostgreSQL schema')
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + self.schema + '" CASCADE'))
        self.admin.dispose()


if __name__ == '__main__':
    unittest.main()
