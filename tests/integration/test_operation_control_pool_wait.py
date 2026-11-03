"""The production PostgreSQL backend obeys the same pool-capacity bound."""
import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, text

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.unit.test_database_pool_wait import PoolWaitContract
from tests.unit.test_operation_control_reads import seed_operation


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"), "Disposable PostgreSQL URL required")
class PostgresPoolWaitTests(PoolWaitContract, unittest.TestCase):
    def setUp(self):
        url = os.environ["MJOBS_TEST_POSTGRES_URL"]
        self.schema = "mjobs_pool_wait_" + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.addCleanup(self.drop_schema)
        self.store = Store(url)
        self.store.engine = self.store.engine.execution_options(schema_translate_map={None: self.schema})
        self.addCleanup(self.store.engine.dispose)
        m.operations.create(self.store.engine)
        seed_operation(self.store)

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
