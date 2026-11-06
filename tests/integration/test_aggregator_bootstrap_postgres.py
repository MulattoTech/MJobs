"""Existing bootstrap oracles on real isolated PostgreSQL, controlled HTTP only.

Set MJOBS_TEST_POSTGRES_URL to an explicitly disposable loopback mjobs_test*
database. Each fixture owns one Alembic-migrated schema. The collection and
worker assertions are inherited unchanged; only the SQLite lock probe has a
PostgreSQL equivalent. Worker tests require the actual product dispatcher.
No RQ server, public provider request or full journey is exercised here.
"""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import configure_source
from tests.integration import test_aggregator_bootstrap_collection as collection
from tests.integration import test_aggregator_bootstrap_worker as worker
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_spine import PROFILE


class _PostgresCollectionFixture(unittest.TestCase):
    """Setup-only replacement for worker tests' nested collection fixtures."""
    collect = collection.AggregatorBootstrapCollectionTests.collect
    pending = collection.AggregatorBootstrapCollectionTests.pending
    parent_operation = collection.AggregatorBootstrapCollectionTests.parent_operation

    def setUp(self):
        url = make_url(os.environ['MJOBS_TEST_POSTGRES_URL'])
        if (url.get_backend_name() != 'postgresql' or url.host not in {'localhost', '127.0.0.1', '::1'}
                or not url.database or not (url.database == 'mjobs_test' or url.database.startswith('mjobs_test_'))
                or url.query):
            raise ValueError('An explicit loopback disposable mjobs_test database URL is required')
        self._schema = 'aggregator_bootstrap_' + uuid4().hex
        self._admin = create_engine(url, connect_args={'connect_timeout': 5}, hide_parameters=True)
        self.addCleanup(self._admin.dispose)
        with self._admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self._schema + '"'))
        self.addCleanup(self._drop_owned_schema)
        self.url = url.update_query_dict({'options': '-csearch_path=' + self._schema
            + ' -clock_timeout=5000 -cstatement_timeout=30000'}).render_as_string(hide_password=False)
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL': self.url}):
            command.upgrade(Config('alembic.ini'), 'head')
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.assertEqual(self.store.engine.dialect.name, 'postgresql')
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(deepcopy(PROFILE))
        self.source_id = configure_source(self.store, 'default', provider='remotive')
        self.rows = [deepcopy(RAW_REMOTIVE_PAYLOAD['jobs'][0])]
        self.http_requests = []
        self.after_feed_read = None

    def _drop_owned_schema(self):
        suffix = self._schema.removeprefix('aggregator_bootstrap_')
        if (not self._schema.startswith('aggregator_bootstrap_') or len(suffix) != 32
                or any(char not in '0123456789abcdef' for char in suffix)):
            raise ValueError('Unexpected disposable bootstrap schema')
        with self._admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self._schema + '" CASCADE'))


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'), 'disposable PostgreSQL not configured')
class AggregatorBootstrapCollectionPostgresTests(collection.AggregatorBootstrapCollectionTests):
    setUp = _PostgresCollectionFixture.setUp
    _drop_owned_schema = _PostgresCollectionFixture._drop_owned_schema

    def test_positive_native_hint_parsing_can_lock_source_before_staging(self):
        from services.company_discovery.aggregator_bootstrap import extract_bootstrap_hint
        self.rows[0]['description'] += '<a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply</a>'
        def probe(raw, normalized):
            with self.store.engine.begin() as connection:
                self.assertEqual(connection.execute(select(m.job_sources.c.id).where(
                    m.job_sources.c.id == self.source_id).with_for_update(nowait=True)).scalar_one(), self.source_id)
            return extract_bootstrap_hint(raw, normalized)
        with patch('services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint', side_effect=probe) as extract:
            self.assertEqual(self.collect()['jobs_seen'], 1)
        extract.assert_called_once()
        self.assertEqual(len(self.pending()), 1)

    def test_pure_hint_parsing_does_not_hold_the_database_write_lock(self):
        def probe(_raw, _normalized):
            # A distinct connection must be able to lock the exact source row
            # without waiting while pure hint parsing runs outside ingestion.
            with self.store.engine.begin() as connection:
                self.assertEqual(connection.execute(select(m.job_sources.c.id).where(
                    m.job_sources.c.id == self.source_id).with_for_update(nowait=True)).scalar_one(), self.source_id)
            return None
        with patch('services.company_discovery.aggregator_bootstrap.extract_bootstrap_hint', side_effect=probe) as extract:
            self.assertEqual(self.collect()['jobs_seen'], 1)
        extract.assert_called_once()
        self.assertEqual(self.pending(), [])


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'), 'disposable PostgreSQL not configured')
class AggregatorBootstrapWorkerPostgresTests(worker.AggregatorBootstrapWorkerTests):
    def setUp(self):
        # Existing worker tests create additional collection fixtures inside
        # several test methods. Redirect only that test factory, not Store,
        # production dispatch, authorization, leases, or ingestion behavior.
        factory = patch.object(collection, 'AggregatorBootstrapCollectionTests', _PostgresCollectionFixture)
        factory.start()
        self.addCleanup(factory.stop)
        super().setUp()
        self.assertEqual(self.case.store.engine.dialect.name, 'postgresql')
