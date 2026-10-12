"""Actual PostgreSQL import, identity and revocation acceptance in isolated schemas."""
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from packages.persistence import models as m
from packages.persistence.store import upsert
from services.company_discovery.ats_enrollment import bound_company_source
from services.company_discovery.domains import lock_domain_evidence
from services.company_discovery.source_host_locks import lock_company_source_hosts
from services.company_discovery.sources import upsert_company_job_source

from tests.integration import test_greenhouse_autodiscovery as contract

POSTGRES_URL = os.environ.get('MJOBS_TEST_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'Set MJOBS_TEST_POSTGRES_URL to a disposable loopback mjobs_test database')
class GreenhouseAutodiscoveryPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith('postgresql') or url.host not in ('localhost', '127.0.0.1', '::1')
                or not url.database or not (url.database == 'mjobs_test' or url.database.startswith('mjobs_test_'))):
            raise RuntimeError('Disposable loopback mjobs_test PostgreSQL database required')
        self.schema = 'mjobs_greenhouse_' + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        scoped = url.update_query_dict({'options': '-csearch_path=' + self.schema + ' -clock_timeout=5000 -cstatement_timeout=30000'})
        database_url = scoped.render_as_string(hide_password=False)
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL': database_url}):
            command.upgrade(Config('alembic.ini'), 'head')
        self.case = contract.GreenhouseAutodiscoveryTests()
        self.case.database_url = database_url
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_verified_ats_enrollment_reload_and_recurrence_on_postgresql(self):
        self.case.test_verified_ats_enrollment_import_reload_and_recurring_collection()

    def test_aggregator_and_first_party_dedupe_on_postgresql(self):
        self.case.test_aggregator_posting_and_first_party_inventory_share_canonical_job()

    def test_domain_revocation_prevents_import_on_postgresql(self):
        self.case.test_revocation_during_fetch_prevents_import_and_further_scheduling()

    def test_disabled_source_is_not_scheduled_or_imported_on_postgresql(self):
        self.case.test_disabled_bound_source_is_not_scheduled_or_imported()

    def test_import_binding_blocks_concurrent_foreign_owner_source_write(self):
        self.assertEqual(self.case.discover()['status'], 'COMPLETED')
        with self.case.store.engine.begin() as connection:
            upsert(connection, m.companies, {'id': 'foreign-employer', 'name': 'Other Employer', 'domain': ''})
        with self.case.store.engine.begin() as connection:
            collector = connection.execute(select(m.job_sources)).mappings().one()
            bound_company_source(self.case.store, connection, collector, lock=True)
            with self.assertRaises(OperationalError) as failure:
                with self.case.store.engine.begin() as other:
                    other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                    upsert_company_job_source(other, company_id='foreign-employer', provider='greenhouse',
                        tenant_identifier='example', career_url='https://foreign.example/careers',
                        public_api_url='https://boards-api.greenhouse.io/v1/boards/example/jobs',
                        collection_strategy='API')
            self.assertEqual(failure.exception.orig.sqlstate, '55P03')
        with self.case.store.engine.connect() as connection:
            self.assertEqual(len(list(connection.execute(select(m.company_sources)).mappings())), 1)

    def test_binding_waits_for_domain_before_acquiring_source_hosts(self):
        self.assertEqual(self.case.discover()['status'], 'COMPLETED')
        with self.case.store.engine.connect() as connection:
            collector = dict(connection.execute(select(m.job_sources)).mappings().one())
        reached_domain = threading.Event()
        def observed_domain(connection, host):
            reached_domain.set()
            return lock_domain_evidence(connection, host)
        def bind():
            with self.case.store.engine.begin() as connection:
                return bound_company_source(self.case.store, connection, collector, lock=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.case.store.engine.begin() as owner:
                lock_domain_evidence(owner, 'employer.example')
                with patch('services.company_discovery.public_resolution.lock_domain_evidence', observed_domain):
                    future = pool.submit(bind)
                    self.assertTrue(reached_domain.wait(5), 'Binding did not reach domain lock')
                    # The worker is waiting on the real domain lock. It must not
                    # already hold either source-host lock in the reverse order.
                    with self.case.store.engine.begin() as probe:
                        probe.execute(text("SET LOCAL lock_timeout = '200ms'"))
                        lock_company_source_hosts(probe, (
                            'https://boards-api.greenhouse.io/v1/boards/example/jobs',
                            'https://employer.example/careers'))
            self.assertEqual(future.result(timeout=5)['company_id'], 'canonical-employer')
