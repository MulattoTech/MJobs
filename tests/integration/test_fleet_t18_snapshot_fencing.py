"""Consistent read-scope contracts for profile backup snapshots."""
import copy
from pathlib import Path
import tempfile
import unittest

from tests.integration.test_spine import PROFILE
from packages.persistence.store import Store


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

    def profile_at(self, store, connection):
        return store.profile("t18-snapshot-profile", connection=connection)

    def updated_profile(self, name, fact_id):
        updated = copy.deepcopy(self.profile)
        updated.update(name=name, email=f"{fact_id}@example.test")
        updated["facts"] = [dict(self.profile["facts"][0], id=fact_id,
                                  text=f"Synthetic {name} fact")]
        return updated

    def test_wal_snapshot_keeps_profile_version_and_facts_consistent_across_commit(self):
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

        reopened = Store(self.url)
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

        reopened = Store(self.url)
        try:
            saved = reopened.profile("t18-snapshot-profile")
            self.assertEqual(saved["version"], next_version)
            self.assertEqual(saved["name"], "Synthetic saved after interruption")
            self.assertEqual([fact["fact_id"] for fact in saved["facts"]],
                             ["after-interruption-fact"])
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
