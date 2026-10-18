"""Run the public acquisition/refresh contracts on an admitted disposable schema."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from packages.persistence.store import Store
from tests.integration import test_official_domain_acquisition as contract


POSTGRES_URL = os.environ.get('MJOBS_TEST_POSTGRES_URL')


class OfficialDomainAcquisitionSchemaBoundaryTests(unittest.TestCase):
    def test_sqlite_fixture_initializes_its_actual_schema(self):
        case = contract.OfficialDomainAcquisitionTests()
        case.store = Store('sqlite://')
        self.addCleanup(case.store.engine.dispose)
        case._prepare_schema()
        self.assertEqual(case.store.count('career_profiles'), 0)

    def test_postgresql_fixture_preserves_external_alembic_boundary(self):
        # The guard needs only the URL, so exercise the real Store method without
        # requiring a local PG driver/server or mocking a migration result.
        case = contract.OfficialDomainAcquisitionTests()
        case.store = Store.__new__(Store)
        case.store.url = 'postgresql+psycopg://synthetic@127.0.0.1:1/mjobs_test_boundary'
        with self.assertRaisesRegex(ValueError, 'Use alembic upgrade head'):
            case.store.migrate()
        # Shared setup must leave the PG schema prepared by the wrapper alone.
        case._prepare_schema()


@unittest.skipUnless(POSTGRES_URL, 'NOT_RUN: disposable loopback MJOBS_TEST_POSTGRES_URL absent')
class OfficialDomainAcquisitionPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith('postgresql')
                or url.host not in {'localhost', '127.0.0.1', '::1'}
                or url.port not in (None, 5432) or url.query
                or not url.database or not (url.database == 'mjobs_test' or url.database.startswith('mjobs_test_'))):
            raise RuntimeError('Requires disposable loopback PostgreSQL mjobs_test database without URL options')
        self.schema = 'mjobs_domain_' + uuid4().hex
        self.admin = create_engine(url, connect_args={'connect_timeout': 5})
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self._drop_owned_schema)
        scoped = url.update_query_dict({'options': '-csearch_path=' + self.schema +
            ' -clock_timeout=5000 -cstatement_timeout=30000'})
        database_url = scoped.render_as_string(hide_password=False)
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL': database_url}):
            command.upgrade(Config('alembic.ini'), 'head')
        self.case = contract.OfficialDomainAcquisitionTests()
        self.case.database_url = database_url
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def _drop_owned_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))

    def test_public_native_acquisition_retention_scheduled_refresh_and_readback(self):
        self.case.test_reciprocal_native_job_page_enrolls_and_survives_reopen_and_replay()

    def test_revoked_acquired_domain_blocks_native_refresh_without_egress(self):
        self.case.test_revoked_acquired_domain_blocks_subsequent_native_refresh_before_egress()
