"""Durable profile creation and optimistic-update fencing regressions."""
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import tempfile
import threading
import unittest

from sqlalchemy import event, select

from packages.persistence import models as m
from packages.persistence.store import ProfileVersionConflict, Store
from tests.integration.test_spine import PROFILE


class ProfileWriteFencingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t18-fencing-")
        self.addCleanup(self.directory.cleanup)
        self.url = f"sqlite:///{(Path(self.directory.name) / 'fencing.db').as_posix()}"
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()

    def competing_profile(self, name, fact_id):
        profile = copy.deepcopy(PROFILE)
        profile.update(id="t18-synthetic-profile", name=name,
                       email=f"{fact_id}@example.test", answers=[])
        profile["facts"] = [dict(PROFILE["facts"][0], id=fact_id,
                                 text=f"Synthetic fact from {name}")]
        return profile

    def wait_for_both_initial_reads(self, stores, winner_saved, *, delay_loser_after_query):
        both_read = threading.Barrier(2)

        def install(store, delayed):
            def after_query(_connection, _cursor, statement, _parameters,
                            _context, _executemany):
                if statement.lstrip().lower().startswith("select career_profiles.version"):
                    both_read.wait(timeout=5)
                    if delayed and delay_loser_after_query and not winner_saved.wait(timeout=5):
                        raise TimeoutError("winning profile save did not finish")

            event.listen(store.engine, "after_cursor_execute", after_query)

        install(stores[0], delayed=False)
        install(stores[1], delayed=True)

    def race_saves(self, profiles, *, expected_versions=None, delay_loser_after_query=True):
        first = self.store
        second = Store(self.url)
        self.addCleanup(second.engine.dispose)
        winner_saved = threading.Event()
        self.wait_for_both_initial_reads(
            (first, second), winner_saved,
            delay_loser_after_query=delay_loser_after_query,
        )
        if not delay_loser_after_query:
            original_profile = second.profile

            def delayed_previous(*args, **kwargs):
                previous = original_profile(*args, **kwargs)
                if not winner_saved.wait(timeout=5):
                    raise TimeoutError("winning profile update did not finish")
                return previous

            second.profile = delayed_previous
        expected_versions = expected_versions or (None, None)

        def save(store, profile, expected_version, index):
            try:
                result = store.save_profile(profile, expected_version=expected_version)
            except ProfileVersionConflict:
                return ("conflict", None)
            if index == 0:
                winner_saved.set()
            return ("saved", result)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(save, first, profiles[0], expected_versions[0], 0),
                executor.submit(save, second, profiles[1], expected_versions[1], 1),
            ]
            return [future.result(timeout=10) for future in futures]

    def test_concurrent_initial_creators_keep_only_the_winner_snapshot_and_audit(self):
        profiles = (
            self.competing_profile("Synthetic winner", "winner-fact"),
            self.competing_profile("Synthetic delayed creator", "loser-fact"),
        )

        results = self.race_saves(profiles)

        self.assertEqual([result[0] for result in results], ["saved", "conflict"])
        winner_version = results[0][1]
        reopened = Store(self.url)
        try:
            saved = reopened.profile("t18-synthetic-profile")
            with reopened.engine.connect() as connection:
                versions = connection.execute(select(m.profile_versions).where(
                    m.profile_versions.c.profile_id == "t18-synthetic-profile")).mappings().all()
                facts = connection.execute(select(m.facts).where(
                    m.facts.c.profile_id == "t18-synthetic-profile")).mappings().all()
                audits = connection.execute(select(m.audit).where(
                    m.audit.c.entity_id == "t18-synthetic-profile",
                    m.audit.c.action == "PROFILE_SAVED")).mappings().all()
            self.assertEqual(saved["name"], "Synthetic winner")
            self.assertEqual(saved["version"], winner_version)
            self.assertEqual([row["version"] for row in versions], [winner_version])
            self.assertEqual([row["fact_id"] for row in facts], ["winner-fact"])
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]["payload"]["version"], winner_version)
        finally:
            reopened.engine.dispose()

    def test_stale_concurrent_update_cannot_persist_its_snapshot_facts_or_audit(self):
        original = copy.deepcopy(PROFILE)
        original.update(id="t18-synthetic-profile", name="Synthetic original",
                        email="original@example.test")
        initial_version = self.store.save_profile(original)
        profiles = (
            self.competing_profile("Synthetic update winner", "update-winner-fact"),
            self.competing_profile("Synthetic stale update", "stale-update-fact"),
        )

        results = self.race_saves(
            profiles, expected_versions=(initial_version, initial_version),
            delay_loser_after_query=False,
        )

        self.assertEqual([result[0] for result in results], ["saved", "conflict"])
        winner_version = results[0][1]
        reopened = Store(self.url)
        try:
            saved = reopened.profile("t18-synthetic-profile")
            with reopened.engine.connect() as connection:
                versions = connection.execute(select(m.profile_versions).where(
                    m.profile_versions.c.profile_id == "t18-synthetic-profile")).mappings().all()
                facts = connection.execute(select(m.facts).where(
                    m.facts.c.profile_id == "t18-synthetic-profile")).mappings().all()
                audits = connection.execute(select(m.audit).where(
                    m.audit.c.entity_id == "t18-synthetic-profile",
                    m.audit.c.action == "PROFILE_SAVED")).mappings().all()
            self.assertEqual(saved["name"], "Synthetic update winner")
            self.assertEqual(saved["version"], winner_version)
            self.assertEqual({row["version"] for row in versions}, {initial_version, winner_version})
            self.assertEqual({row["fact_id"] for row in facts},
                             {"python", "sql", "update-winner-fact"})
            self.assertEqual(len(audits), 2)
            self.assertEqual({row["payload"]["version"] for row in audits},
                             {initial_version, winner_version})
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
