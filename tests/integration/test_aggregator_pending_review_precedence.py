"""Explicit employer review safely supersedes only an unclaimed bootstrap hint."""
from datetime import datetime, timedelta, timezone
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
from services.company_discovery import requests
from services.company_discovery.requests import approve_discovery_request


class AggregatorPendingReviewPrecedenceTests(unittest.TestCase):
    def setUp(self):
        from tests.integration.test_reviewed_greenhouse_enrollment import ReviewedGreenhouseEnrollmentTests

        self.case = ReviewedGreenhouseEnrollmentTests()
        if hasattr(self, 'database_url'):
            self.case.database_url = self.database_url
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.case.tearDown)
        summary = self.case.collect()
        with self.case.store.engine.connect() as connection:
            self.request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == summary['id'])).mappings().one())
            self.operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.request['operation_id'])).mappings().one())
        self.assertEqual(self.request['authorization'], 'AGGREGATOR_BOOTSTRAP_PENDING')
        self.assertEqual(self.operation['state'], 'QUEUED')

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
