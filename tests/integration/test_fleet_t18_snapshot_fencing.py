"""Consistent read-scope contracts for profile backup snapshots."""
import asyncio
import copy
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.pool import NullPool

from tests.integration.test_spine import PROFILE
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url
from packages.persistence import models as m
from packages.persistence.store import Store

POSTGRES_URL = os.environ.get("MJOBS_TEST_POSTGRES_URL")


class BoundedPostgresSnapshotStore(Store):
    """Real Store methods with a bounded pool for the opt-in PG fixture."""

    def __init__(self, url):
        self.url = url
        self.engine = create_engine(
            url, pool_size=2, max_overflow=0, pool_timeout=3,
            pool_pre_ping=True, connect_args={"connect_timeout": 3},
        )


class ProfileReadSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t18-read-snapshot-")
        self.addCleanup(self.directory.cleanup)
        self.url = f"sqlite:///{(Path(self.directory.name) / 'snapshot.db').as_posix()}"
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        # WAL lets a real writer commit while a reader retains its old view.
        with self.store.engine.connect() as connection:
            mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
            connection.commit()
        self.assertEqual(mode.lower(), "wal")
        self.writer = Store(self.url)
        self.addCleanup(self.writer.engine.dispose)
        self.profile = copy.deepcopy(PROFILE)
        self.profile.update(id="t18-snapshot-profile", name="Synthetic snapshot v1",
                            email="snapshot@example.test", answers=[])
        self.initial_version = self.store.save_profile(self.profile)
        # Exercise the normal profile/access-check read before a later snapshot
        # on the same Store pool.
        self.assertEqual(self.store.profile(self.profile["id"])["version"], self.initial_version)

    def profile_at(self, store, connection):
        return store.profile("t18-snapshot-profile", connection=connection)

    def updated_profile(self, name, fact_id):
        updated = copy.deepcopy(self.profile)
        updated.update(name=name, email=f"{fact_id}@example.test")
        updated["facts"] = [dict(self.profile["facts"][0], id=fact_id,
                                  text=f"Synthetic {name} fact")]
        return updated

    def make_reopened_store(self):
        return Store(self.url)

    def test_snapshot_keeps_profile_version_and_facts_consistent_across_commit(self):
        writer_profile = self.updated_profile("Synthetic snapshot v2", "snapshot-v2-fact")

        with self.store.read_snapshot() as connection:
            first = self.profile_at(self.store, connection)
            writer_version = self.writer.save_profile(
                writer_profile, expected_version=self.initial_version)
            second = self.profile_at(self.store, connection)

        self.assertEqual(first["version"], self.initial_version)
        self.assertEqual(second["version"], self.initial_version)
        self.assertEqual(first, second)
        self.assertEqual([fact["fact_id"] for fact in second["facts"]], ["python", "sql"])

        reopened = self.make_reopened_store()
        try:
            with reopened.read_snapshot() as connection:
                current = self.profile_at(reopened, connection)
            self.assertEqual(current["version"], writer_version)
            self.assertEqual(current["name"], "Synthetic snapshot v2")
            self.assertEqual([fact["fact_id"] for fact in current["facts"]],
                             ["snapshot-v2-fact"])
        finally:
            reopened.engine.dispose()

    def test_exception_releases_snapshot_before_later_save_and_fresh_readback(self):
        writer_profile = self.updated_profile("Synthetic committed during interruption",
                                              "interruption-fact")
        with self.assertRaisesRegex(RuntimeError, "injected snapshot interruption"):
            with self.store.read_snapshot() as connection:
                original = self.profile_at(self.store, connection)
                committed_version = self.writer.save_profile(
                    writer_profile, expected_version=self.initial_version)
                self.assertEqual(original["version"], self.initial_version)
                raise RuntimeError("injected snapshot interruption")

        # The same Store/pool must not retain a stale transaction after the
        # exceptional exit; ordinary writes and reads can proceed immediately.
        after_interruption = self.store.profile("t18-snapshot-profile")
        self.assertEqual(after_interruption["version"], committed_version)
        next_profile = self.updated_profile("Synthetic saved after interruption",
                                            "after-interruption-fact")
        next_version = self.store.save_profile(
            next_profile, expected_version=committed_version)

        reopened = self.make_reopened_store()
        try:
            saved = reopened.profile("t18-snapshot-profile")
            self.assertEqual(saved["version"], next_version)
            self.assertEqual(saved["name"], "Synthetic saved after interruption")
            self.assertEqual([fact["fact_id"] for fact in saved["facts"]],
                             ["after-interruption-fact"])
        finally:
            reopened.engine.dispose()

    def test_cancelled_error_releases_snapshot_before_next_save_and_readback(self):
        with self.assertRaisesRegex(asyncio.CancelledError, "injected snapshot cancellation"):
            with self.store.read_snapshot() as connection:
                profile = self.profile_at(self.store, connection)
                self.assertEqual(profile["version"], self.initial_version)
                raise asyncio.CancelledError("injected snapshot cancellation")

        updated = self.updated_profile("Synthetic saved after cancellation",
                                       "after-cancellation-fact")
        version = self.store.save_profile(updated, expected_version=self.initial_version)

        reopened = self.make_reopened_store()
        try:
            saved = reopened.profile("t18-snapshot-profile")
            self.assertEqual(saved["version"], version)
            self.assertEqual(saved["name"], "Synthetic saved after cancellation")
            self.assertEqual([fact["fact_id"] for fact in saved["facts"]],
                             ["after-cancellation-fact"])
        finally:
            reopened.engine.dispose()

    def test_nested_snapshot_exception_does_not_end_outer_read_scope(self):
        writer_profile = self.updated_profile("Synthetic nested snapshot v2",
                                              "nested-snapshot-v2-fact")

        with self.store.read_snapshot() as outer_connection:
            outer_before = self.profile_at(self.store, outer_connection)
            outer_isolation = outer_connection.get_isolation_level()
            writer_version = self.writer.save_profile(
                writer_profile, expected_version=self.initial_version)

            with self.assertRaisesRegex(RuntimeError, "injected nested snapshot interruption"):
                with self.store.read_snapshot() as inner_connection:
                    inner = self.profile_at(self.store, inner_connection)
                    self.assertEqual(inner["version"], writer_version)
                    self.assertEqual([fact["fact_id"] for fact in inner["facts"]],
                                     ["nested-snapshot-v2-fact"])
                    raise RuntimeError("injected nested snapshot interruption")

            outer_after = self.profile_at(self.store, outer_connection)
            self.assertEqual(outer_connection.get_isolation_level(), outer_isolation)

        self.assertEqual(outer_before["version"], self.initial_version)
        self.assertEqual(outer_after, outer_before)
        self.assertEqual([fact["fact_id"] for fact in outer_after["facts"]], ["python", "sql"])

    def test_snapshot_exception_does_not_commit_or_rollback_independent_writer(self):
        with self.writer.engine.connect() as write_connection:
            write_transaction = write_connection.begin()
            write_connection.execute(update(m.profiles).where(
                m.profiles.c.id == "t18-snapshot-profile").values(name="Uncommitted writer value"))

            with self.assertRaisesRegex(RuntimeError, "injected snapshot interruption"):
                with self.store.read_snapshot() as snapshot_connection:
                    self.profile_at(self.store, snapshot_connection)
                    raise RuntimeError("injected snapshot interruption")

            self.assertTrue(write_transaction.is_active)
            self.assertEqual(write_connection.execute(select(m.profiles.c.name).where(
                m.profiles.c.id == "t18-snapshot-profile")).scalar_one(),
                "Uncommitted writer value")
            with self.store.engine.connect() as outside_connection:
                self.assertEqual(outside_connection.execute(select(m.profiles.c.name).where(
                    m.profiles.c.id == "t18-snapshot-profile")).scalar_one(),
                    "Synthetic snapshot v1")

            write_transaction.rollback()

        self.assertEqual(self.store.profile("t18-snapshot-profile")["name"],
                         "Synthetic snapshot v1")

    def test_in_memory_shared_transaction_fails_closed_before_begin(self):
        memory_store = Store("sqlite:///:memory:")
        self.addCleanup(memory_store.engine.dispose)
        memory_store.migrate()
        profile = copy.deepcopy(self.profile)
        profile.update(id="t18-memory-snapshot-profile", name="Synthetic memory profile",
                       email="memory@example.test")
        version = memory_store.save_profile(profile)
        # Model the usual profile/access-check read before a later snapshot
        # request. In-memory SQLite may hand both scopes the same DBAPI handle.
        self.assertEqual(memory_store.profile(profile["id"])["version"], version)

        with memory_store.engine.connect() as outer_connection:
            outer_transaction = outer_connection.begin()
            outer_connection.execute(update(m.profiles).where(
                m.profiles.c.id == profile["id"]).values(name="Uncommitted outer value"))
            dbapi_connection = outer_connection.connection.driver_connection
            self.assertTrue(dbapi_connection.in_transaction)
            explicit_begins = []

            def record_explicit_begin(_conn, _cursor, statement, _parameters,
                                      _context, _executemany):
                if statement.strip().upper() == "BEGIN":
                    explicit_begins.append(statement)

            event.listen(memory_store.engine, "before_cursor_execute", record_explicit_begin)
            try:
                with self.assertRaises(RuntimeError):
                    with memory_store.read_snapshot():
                        self.fail("snapshot should reject the active shared DBAPI transaction")
            finally:
                event.remove(memory_store.engine, "before_cursor_execute", record_explicit_begin)

            self.assertEqual(explicit_begins, [])
            self.assertTrue(outer_transaction.is_active)
            self.assertTrue(dbapi_connection.in_transaction)
            self.assertEqual(outer_connection.execute(select(m.profiles.c.name).where(
                m.profiles.c.id == profile["id"])).scalar_one(), "Uncommitted outer value")
            outer_transaction.commit()

        with memory_store.engine.connect() as fresh_connection:
            persisted_name = fresh_connection.execute(select(m.profiles.c.name).where(
                m.profiles.c.id == profile["id"])).scalar_one()
        self.assertEqual(persisted_name, "Uncommitted outer value")


@unittest.skipUnless(POSTGRES_URL,
                     "Requires disposable loopback MJOBS_TEST_POSTGRES_URL")
class ProfileReadSnapshotPostgresTests(ProfileReadSnapshotTests):
    """Reuse snapshot contracts on a unique schema in the disposable PG fixture."""

    def setUp(self):
        url = _disposable_postgres_url(POSTGRES_URL)
        schema = "mjobs_t18_snapshot_" + uuid4().hex
        admin = create_engine(url, poolclass=NullPool,
                              connect_args={"connect_timeout": 3})
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))

        def cleanup_schema():
            try:
                with admin.begin() as connection:
                    connection.execute(text('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE'))
            finally:
                admin.dispose()

        self.addCleanup(cleanup_schema)
        self.url = url.update_query_dict({
            "options": "-csearch_path=" + schema
            + " -clock_timeout=3000 -cstatement_timeout=15000"
            + " -capplication_name=" + schema,
        }).render_as_string(hide_password=False)
        self.store = BoundedPostgresSnapshotStore(self.url)
        self.addCleanup(self.store.engine.dispose)
        from packages.persistence import resume_workspace  # noqa: F401
        m.metadata.create_all(self.store.engine)
        self.writer = BoundedPostgresSnapshotStore(self.url)
        self.addCleanup(self.writer.engine.dispose)
        self.profile = copy.deepcopy(PROFILE)
        self.profile.update(id="t18-snapshot-profile", name="Synthetic snapshot v1",
                            email="snapshot@example.test", answers=[])
        self.initial_version = self.store.save_profile(self.profile)
        self.assertEqual(self.store.profile(self.profile["id"])["version"], self.initial_version)

    def make_reopened_store(self):
        return BoundedPostgresSnapshotStore(self.url)

    @unittest.skip("SQLite in-memory-only shared DBAPI guard")
    def test_in_memory_shared_transaction_fails_closed_before_begin(self):
        pass

    def test_snapshot_scope_does_not_leak_repeatable_read_to_pool(self):
        with self.store.engine.connect() as connection:
            default_isolation = connection.execute(text("SHOW transaction_isolation")).scalar_one()
            backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        self.assertEqual(default_isolation.lower(), "read committed")

        with self.store.read_snapshot() as connection:
            snapshot_isolation = connection.execute(text("SHOW transaction_isolation")).scalar_one()
            snapshot_backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            self.assertEqual(snapshot_isolation.lower(), "repeatable read")
            self.assertEqual(snapshot_backend_pid, backend_pid)

        with self.store.engine.connect() as connection:
            after_success = connection.execute(text("SHOW transaction_isolation")).scalar_one()
            after_success_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        self.assertEqual(after_success.lower(), "read committed")
        self.assertEqual(after_success_pid, backend_pid)

        with self.assertRaisesRegex(RuntimeError, "injected snapshot interruption"):
            with self.store.read_snapshot() as connection:
                interrupted_isolation = connection.execute(
                    text("SHOW transaction_isolation")).scalar_one()
                self.assertEqual(interrupted_isolation.lower(), "repeatable read")
                raise RuntimeError("injected snapshot interruption")

        with self.store.engine.connect() as connection:
            after_exception = connection.execute(text("SHOW transaction_isolation")).scalar_one()
            after_exception_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        self.assertEqual(after_exception.lower(), "read committed")
        self.assertEqual(after_exception_pid, backend_pid)


if __name__ == "__main__":
    unittest.main()
