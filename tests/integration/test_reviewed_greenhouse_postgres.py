"""Actual PostgreSQL reviewed-employer enrollment, recurrence and revocation."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from packages.persistence import models as m
from services.company_discovery.ats_enrollment import bound_company_source
from tests.integration import test_reviewed_greenhouse_enrollment as contract

POSTGRES_URL = os.environ.get('MJOBS_TEST_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'Set MJOBS_TEST_POSTGRES_URL to disposable loopback mjobs_test PostgreSQL')
class ReviewedGreenhousePostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith('postgresql') or url.host not in ('localhost', '127.0.0.1', '::1')
                or not url.database or not (url.database == 'mjobs_test' or url.database.startswith('mjobs_test_'))):
            raise RuntimeError('Disposable loopback mjobs_test PostgreSQL database required')
        self.schema = 'mjobs_reviewed_ats_' + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        scoped = url.update_query_dict({'options': '-csearch_path=' + self.schema + ' -clock_timeout=5000 -cstatement_timeout=30000'})
        database_url = scoped.render_as_string(hide_password=False)
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL': database_url}):
            command.upgrade(Config('alembic.ini'), 'head')
        self.case = contract.ReviewedGreenhouseEnrollmentTests()
        self.case.database_url = database_url
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_reviewed_employer_enrollment_dedupe_reload_and_recurrence(self):
        self.case.test_reviewed_new_employer_enrolls_imports_and_reloads_recurring_binding()

    def test_revocation_blocks_scheduling_and_egress(self):
        self.case.test_revoked_review_blocks_scheduling_and_egress()

    def test_revocation_during_fetch_blocks_import(self):
        self.case.test_review_revocation_during_fetch_prevents_import_without_provider_backoff()

    def test_changed_review_or_binding_cannot_authorize_collection(self):
        self.case.test_changed_authority_and_binding_fail_closed()

    def test_source_pause_is_preserved(self):
        self.case.test_confirmation_does_not_override_paused_enrolled_source()

    def test_import_holds_review_row_and_trigger_collector_until_commit(self):
        request, source = self.case.enroll()
        with self.case.store.engine.begin() as connection:
            collector = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source['collector_source_id'])).mappings().one()
            bound_company_source(self.case.store, connection, collector, lock=True)
            for table, row_id, values in (
                (m.company_discovery_requests, request['id'], {'domain_evidence': []}),
                (m.job_sources, self.case.source, {'enabled': False}),
            ):
                with self.subTest(table=table.name):
                    with self.assertRaises(OperationalError) as failure:
                        with self.case.store.engine.begin() as other:
                            other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                            other.execute(update(table).where(table.c.id == row_id).values(**values))
                    self.assertEqual(failure.exception.orig.sqlstate, '55P03')
