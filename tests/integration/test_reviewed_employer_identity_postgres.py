"""Opt-in PostgreSQL continuity/locking proof; isolated schemas, synthetic data only.

This checks PostgreSQL behavior using test metadata, not Alembic migration acceptance.
"""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_reviewed_employer_identity_continuity as continuity
from services.company_discovery.requests import approve_discovery_request, authorize_discovery_request

POSTGRES_URL = os.environ.get('MJOBS_REVIEWED_IDENTITY_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'Set MJOBS_REVIEWED_IDENTITY_POSTGRES_URL to a disposable loopback mjobs_test_* database')
class ReviewedIdentityPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith('postgresql') or url.host not in ('localhost', '127.0.0.1', '::1')
                or not url.database or not url.database.startswith('mjobs_test_')):
            raise RuntimeError('A disposable loopback mjobs_test_* PostgreSQL database is required')
        self.schema = 'mjobs_identity_' + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.cleanup_schema)
        self.url = url.update_query_dict({'options': '-csearch_path=' + self.schema}).render_as_string(hide_password=False)
        target_url = self.url
        class TestStore(Store):
            def __init__(self, ignored):
                super().__init__(target_url)
            def migrate(self):
                from packages.persistence import resume_workspace  # noqa: F401
                m.metadata.create_all(self.engine)
        self.case = continuity.ReviewedEmployerIdentityContinuityTests()
        with patch.object(continuity, 'Store', TestStore):
            self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def cleanup_schema(self):
        # Only the generated schema in the explicitly validated disposable database.
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_aggregator_review_worker_retains_identity_and_both_posting_sources(self):
        self.case.test_remotive_intake_review_and_direct_feed_keep_company_identity_and_route_provenance()

    def test_revocation_after_enrollment_prevents_import(self):
        self.case.test_revocation_between_enrollment_and_import_leaves_only_disabled_candidate_source()

    def test_foreign_source_owner_cannot_be_claimed(self):
        self.case.test_company_source_owned_by_another_employer_blocks_identity_confirmation()

    def test_default_candidate_does_not_claim_employer_identity(self):
        self.case.test_default_and_false_confirmation_keep_candidate_identity_isolated()

    def test_import_authorization_holds_collector_row_lock_until_transaction_ends(self):
        case = self.case
        request = case.collect()
        queued = approve_discovery_request(case.store, request['id'], 'https://employer.example/',
                                          request['revision'], confirm_company_identity=True)
        with case.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == queued['operation_id'])).mappings().one())
        with case.store.engine.begin() as holder:
            authorize_discovery_request(case.store, operation, connection=holder)
            with self.assertRaises(OperationalError) as error:
                with case.store.engine.begin() as contender:
                    contender.execute(text("SET LOCAL lock_timeout = '200ms'"))
                    contender.execute(m.job_sources.update().where(m.job_sources.c.id == case.source).values(enabled=False))
            self.assertEqual(getattr(error.exception.orig, 'sqlstate', None), '55P03')
        # Released after commit; disabling now succeeds and the next guard rejects it.
        with case.store.engine.begin() as connection:
            connection.execute(m.job_sources.update().where(m.job_sources.c.id == case.source).values(enabled=False))
        with self.assertRaisesRegex(Exception, 'DISCOVERY_SOURCE_DISABLED'):
            authorize_discovery_request(case.store, operation)

    def test_attestation_blocks_new_conflicting_subdomain_source_until_guard_transaction_ends(self):
        case = self.case
        request = case.collect()
        queued = approve_discovery_request(case.store, request['id'], 'https://employer.example/',
                                          request['revision'], confirm_company_identity=True)
        with case.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(id='concurrent-other', name='Different Employer', domain=''))
        with case.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == queued['operation_id'])).mappings().one())
        def insert_foreign(connection):
            continuity.upsert_company_job_source(connection, company_id='concurrent-other', provider='rss',
                tenant_identifier='concurrent-other', career_url='https://careers.employer.example/',
                feed_url='https://careers.employer.example/jobs.json', collection_strategy='RSS', enabled=True)
        with case.store.engine.begin() as holder:
            authorize_discovery_request(case.store, operation, connection=holder)
            with self.assertRaises(OperationalError) as error:
                with case.store.engine.begin() as contender:
                    contender.execute(text("SET LOCAL lock_timeout = '200ms'"))
                    insert_foreign(contender)
            self.assertEqual(getattr(error.exception.orig, 'sqlstate', None), '55P03')
        with case.store.engine.begin() as connection:
            insert_foreign(connection)
        with self.assertRaisesRegex(Exception, 'DISCOVERY_COMPANY_IDENTITY_CONFLICT'):
            authorize_discovery_request(case.store, operation)
