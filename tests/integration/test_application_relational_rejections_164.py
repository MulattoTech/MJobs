"""MJ-REQ-163: raw application writes must fail atomically at the DB boundary.

SQLite is always disposable. PostgreSQL uses the existing loopback test-database
guard and an owned UUID schema only when MJOBS_TEST_POSTGRES_URL is configured.
These tests prove selected relational invariants, not all impossible domain states.
"""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, event, null, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from packages.persistence import models as m
from tests.integration import test_profile_recovery_history_migration as guarded_pg


STAMP = "2026-10-25T12:00:00Z"
GRAPH = (m.profiles, m.companies, m.jobs, m.routes, m.applications,
         m.application_route_choices, m.documents, m.attempts)
REQUIRED = ("job_id", "route_id", "profile_id", "profile_version", "state", "target",
            "fact_ids", "trace", "document_version", "created_at")
ERROR_CODES = {
    "sqlite": {"fk": "SQLITE_CONSTRAINT_FOREIGNKEY", "unique": "SQLITE_CONSTRAINT_UNIQUE",
               "null": "SQLITE_CONSTRAINT_NOTNULL", "check": "SQLITE_CONSTRAINT_CHECK"},
    "postgresql": {"fk": "23503", "unique": "23505", "null": "23502", "check": "23514"},
}


class ApplicationRelationalRejectionTests(unittest.TestCase):
    expected_dialect = "sqlite"

    def database_url(self, directory, filename):
        return f"sqlite:///{(Path(directory) / filename).as_posix()}"

    def _engine(self):
        engine = create_engine(self.url, poolclass=NullPool)
        if engine.dialect.name == "sqlite":
            @event.listens_for(engine, "connect")
            def enable_foreign_keys(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")
        return engine

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-relational-164-")
        self.addCleanup(directory.cleanup)
        self.url = self.database_url(directory.name, "constraints.db")
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        # env.py prefers MJOBS_DATABASE_URL: never allow an ambient personal DB.
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": self.url}):
            command.upgrade(config, "head")
        self.engine = self._engine()
        self.addCleanup(self.engine.dispose)
        self.assertEqual(self.engine.dialect.name, self.expected_dialect)
        with self.engine.connect() as connection:
            self._assert_connection_scope(connection)
            self.assertEqual(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                             ScriptDirectory.from_config(config).get_current_head())
        self.recovery_count = 0
        self._seed_graph()

    def _assert_connection_scope(self, connection):
        if self.expected_dialect == "sqlite":
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
        else:
            self.assertEqual(connection.execute(text("SELECT current_schema()")).scalar_one(), self.schema)

    @staticmethod
    def _application(identifier="candidate", **changes):
        values = dict(id=identifier, job_id="job-b", route_id="route-b", profile_id="profile-a",
                      profile_version="v1", state="DRAFT", target="MOCK", fact_ids=[], trace={},
                      document_version=1, confirmation=null(), created_at=STAMP, submitted_at=null())
        values.update(changes)
        return values

    def _seed_graph(self):
        with self.engine.begin() as connection:
            for suffix in ("a", "b"):
                connection.execute(m.profiles.insert().values(
                    id=f"profile-{suffix}", name=f"Synthetic {suffix}",
                    email=f"fixture-{suffix}@example.test", version="v1"))
            connection.execute(m.companies.insert().values(
                id="company", name="Original employer", domain="fixture.example.test"))
            for suffix in ("a", "b"):
                connection.execute(m.jobs.insert().values(
                    id=f"job-{suffix}", company_id="company", requisition=f"REQ-{suffix}",
                    title="Synthetic role", description="Synthetic fixture", skills=[],
                    location="Remote", remote_type="REMOTE", attributes={}))
                connection.execute(m.routes.insert().values(
                    id=f"route-{suffix}", job_id=f"job-{suffix}", company_id="company",
                    provider="mock-ats", tenant="fixture", requisition=f"REQ-{suffix}",
                    url=f"https://fixture.example.test/{suffix}"))
            # The second row makes UPDATE uniqueness distinguishable from PK collision.
            for identifier, profile in (("original", "profile-a"), ("other", "profile-b")):
                connection.execute(m.applications.insert().values(**self._application(
                    identifier, job_id="job-a", route_id="route-a", profile_id=profile)))
            connection.execute(m.application_route_choices.insert().values(
                id="choice", application_id="original", route_id="route-a", underlying_key="fixture-route",
                requested_mode="THIS_ROUTE", state="PLANNED", created_at=STAMP))
            connection.execute(m.documents.insert().values(
                id="document", application_id="original", kind="resume", version=1,
                profile_version="v1", files={}, fact_ids=[], sha256={}, template="fixture", created_at=STAMP))
            connection.execute(m.attempts.insert().values(
                id="attempt", application_id="original", state="RUNNING", steps=[], started_at=STAMP))

    def _snapshot(self, engine=None):
        with (engine or self.engine).connect() as connection:
            self._assert_connection_scope(connection)
            return {table.name: [dict(row) for row in connection.execute(
                select(table).order_by(table.c.id)).mappings()] for table in GRAPH}

    def _assert_rejected(self, statement, category):
        before = self._snapshot()
        with self.assertRaises(IntegrityError) as raised:
            with self.engine.begin() as connection:
                connection.execute(update(m.companies).where(m.companies.c.id == "company")
                                   .values(name="Must roll back"))
                self.assertEqual(connection.execute(select(m.companies.c.name).where(
                    m.companies.c.id == "company")).scalar_one(), "Must roll back")
                connection.execute(statement)
        original_error = raised.exception.orig
        actual = (getattr(original_error, "sqlite_errorname", None) if self.expected_dialect == "sqlite"
                  else getattr(original_error, "sqlstate", None) or getattr(original_error, "pgcode", None))
        self.assertEqual(actual, ERROR_CODES[self.expected_dialect][category])
        self.assertEqual(self._snapshot(), before, "rejection must preserve every original row and child")

        # A real following commit proves the failed transaction did not poison reuse.
        self.recovery_count += 1
        marker = dict(id=f"recovery-{self.recovery_count:03d}", name="Committed after rejection",
                      domain="recovery.example.test")
        with self.engine.begin() as connection:
            connection.execute(m.companies.insert().values(**marker))
        with self.engine.connect() as connection:
            persisted_marker = dict(connection.execute(select(m.companies).where(
                m.companies.c.id == marker["id"])).mappings().one())
        self.assertEqual({key: persisted_marker[key] for key in marker}, marker)
        expected = deepcopy(before)
        expected[m.companies.name] = sorted(expected[m.companies.name] + [persisted_marker], key=lambda row: row["id"])
        self.engine.dispose()
        reopened = self._engine()
        try:
            self.assertEqual(self._snapshot(reopened), expected, "fresh connection must retain only the valid commit")
        finally:
            reopened.dispose()

    def _write(self, operation, **changes):
        if operation == "insert":
            return m.applications.insert().values(**self._application(**changes))
        return update(m.applications).where(m.applications.c.id == "original").values(**changes)

    def test_orphan_job_route_and_profile_are_rejected_on_insert_and_update(self):
        for field in ("job_id", "route_id", "profile_id"):
            for operation in ("insert", "update"):
                with self.subTest(field=field, operation=operation):
                    self._assert_rejected(self._write(operation, **{field: "missing-parent"}), "fk")

    def test_duplicate_job_profile_is_rejected_for_distinct_application_ids(self):
        self._assert_rejected(self._write("insert", job_id="job-a", route_id="route-a"), "unique")
        self._assert_rejected(self._write("update", profile_id="profile-b"), "unique")

    def test_explicit_sql_null_required_fields_are_rejected_on_insert_and_update(self):
        # null() is SQL NULL, not JSON null; None can be serialized by JSON types.
        for field in REQUIRED:
            for operation in ("insert", "update"):
                with self.subTest(field=field, operation=operation):
                    self._assert_rejected(self._write(operation, **{field: null()}), "null")

    def test_invalid_persisted_states_are_rejected_on_insert_and_update(self):
        for state in ("", "draft", "NOT_A_STATE", "APPLICATION_SUBMITTED"):
            for operation in ("insert", "update"):
                with self.subTest(state=state, operation=operation):
                    self._assert_rejected(self._write(operation, state=state), "check")

    def test_referenced_parent_deletion_preserves_application_and_children(self):
        for table, identifier in ((m.jobs, "job-a"), (m.routes, "route-a"),
                                  (m.profiles, "profile-a"), (m.applications, "original")):
            with self.subTest(table=table.name):
                self._assert_rejected(delete(table).where(table.c.id == identifier), "fk")

    def test_valid_pair_and_optional_nulls_commit_and_reopen(self):
        before = self._snapshot()
        with self.engine.begin() as connection:
            connection.execute(self._write("insert"))
        self.engine.dispose()
        reopened = self._engine()
        try:
            after = self._snapshot(reopened)
            candidate = next(row for row in after[m.applications.name] if row["id"] == "candidate")
            self.assertIsNone(candidate["confirmation"])
            self.assertIsNone(candidate["submitted_at"])
            self.assertEqual((candidate["job_id"], candidate["profile_id"]), ("job-b", "profile-a"))
            after[m.applications.name].remove(candidate)
            self.assertEqual(after, before)
        finally:
            reopened.dispose()


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Requires guarded disposable loopback MJOBS_TEST_POSTGRES_URL")
class PostgreSQLApplicationRelationalRejectionTests(ApplicationRelationalRejectionTests):
    expected_dialect = "postgresql"
    database_url = guarded_pg.PostgreSQLProfileRecoveryHistoryMigrationTests.database_url
    cleanup_schema = guarded_pg.PostgreSQLProfileRecoveryHistoryMigrationTests.cleanup_schema

    def setUp(self):
        guarded_pg.PostgreSQLProfileRecoveryHistoryMigrationTests.setUp(self)
        super().setUp()


if __name__ == "__main__":
    unittest.main()
