"""Opt-in queue/result transaction proofs in a disposable PostgreSQL schema."""
import os
import unittest
from uuid import uuid4
from unittest.mock import patch
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from tests.integration import test_durable_uploaded_comparison as contract

POSTGRES_URL = os.environ.get('MJOBS_DURABLE_COMPARISON_POSTGRES_URL')


@unittest.skipUnless(POSTGRES_URL, 'Set MJOBS_DURABLE_COMPARISON_POSTGRES_URL to a disposable loopback mjobs_test_* database')
class DurableComparisonPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith('postgresql') or url.host not in ('localhost', '127.0.0.1', '::1')
                or not url.database or not (url.database == 'mjobs_test' or url.database.startswith('mjobs_test_'))):
            raise RuntimeError('Disposable loopback mjobs_test_* PostgreSQL database required')
        self.schema = 'mjobs_comparison_' + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        scoped = url.update_query_dict({'options': '-csearch_path=' + self.schema + ' -clock_timeout=5000 -cstatement_timeout=30000'})
        database_url = scoped.render_as_string(hide_password=False)
        config = Config('alembic.ini')
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL': database_url}):
            command.upgrade(config, 'head')
        self.case = contract.DurableUploadedComparisonTests()
        self.case.database_url = database_url
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_durable_admission_replay_and_reopen(self):
        self.case.test_admit_disconnect_execute_reopen_and_replay_preserves_result()

    def test_atomic_result_crash_recovery(self):
        self.case.test_result_write_crash_rolls_back_completion_then_recovers_once()

    def test_cancel_completion_race(self):
        self.case.test_completion_cancel_race_discards_computed_result()

    def test_expired_lease_fencing(self):
        self.case.test_expired_unreclaimed_lease_cannot_complete()
