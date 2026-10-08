"""Durable cadence acceptance for independently scheduled saved searches."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.scheduler import schedule_due_sources
from services.discovery.search_profiles import list_search_profiles, save_search_profile
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class SavedSearchScheduleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-saved-search-schedule-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        self.source_id = configure_source(
            self.store, "saved-search-scheduler-fixture", poll_interval=300, jitter_seconds=0)
        self.other_source_id = configure_source(
            self.store, "saved-search-scheduler-other", poll_interval=300, jitter_seconds=0)
        # Make each source due at the first tick. Per-search cadence still respects
        # each source's lower-bound poll interval.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).values(
                last_attempt="2026-10-08T11:55:00+00:00", jitter_seconds=0))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _save_search(self, search_id, profile_id, scheduler=None, *, source_id=None):
        data = {
            "id": search_id,
            "profile_id": profile_id,
            "name": f"Saved search {search_id}",
            "enabled": True,
            "include_titles": ["System Administrator"],
            "source_ids": [source_id or self.source_id],
        }
        if scheduler is not None:
            data["scheduler"] = scheduler
        return save_search_profile(self.store, data)

    def _saved_search(self, search_id):
        return next(row for row in list_search_profiles(self.store)
                    if row["id"] == search_id)

    def _search_operations(self):
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.operations).where(
                m.operations.c.kind == "DISCOVER")).mappings()
            return [dict(row) for row in rows if row["params"].get("search_profile_id")]

    def _schedule_events(self, search_id=None):
        with self.store.engine.connect() as connection:
            statement = select(m.audit).where(
                m.audit.c.action == "SEARCH_PROFILE_SCHEDULE_ENQUEUED").order_by(m.audit.c.timestamp)
            if search_id is not None:
                statement = statement.where(m.audit.c.entity_id == search_id)
            return [dict(row) for row in connection.execute(statement).mappings()]

    def _complete_discoveries(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.kind == "DISCOVER").values(state="SUCCEEDED"))

    def test_scheduler_bounds_and_legacy_default_are_disabled(self):
        # A legacy profile with no scheduler setting stays disabled after persistence.
        self._save_search("legacy-search", "fixture")
        self._save_search("lower-bound", "fixture", {"enabled": True, "interval_seconds": 300})
        self._save_search("upper-bound", "other", {"enabled": True, "interval_seconds": 604800},
                          source_id=self.other_source_id)
        self._save_search("explicitly-disabled", "other", {"enabled": False, "interval_seconds": 300})
        self.assertNotIn("scheduler", self._saved_search("legacy-search")["criteria"])
        self.assertEqual(self._saved_search("lower-bound")["criteria"]["scheduler"],
                         {"enabled": True, "interval_seconds": 300})
        self.assertEqual(self._saved_search("upper-bound")["criteria"]["scheduler"],
                         {"enabled": True, "interval_seconds": 604800})

        at = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        from services.discovery.scheduler import schedule_due_search_profiles
        schedule_due_search_profiles(self.store, at)

        operations = self._search_operations()
        self.assertEqual({row["params"]["search_profile_id"] for row in operations},
                         {"lower-bound", "upper-bound"})
        self.assertEqual({event["entity_id"] for event in self._schedule_events()},
                         {"lower-bound", "upper-bound"})
        self.assertEqual(self.store.count("job_postings"), 0)
        with self.store.engine.connect() as connection:
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == self.source_id)).mappings().one()
        self.assertEqual(source["last_attempt"], "2026-10-08T11:55:00+00:00")

    def test_scheduler_interval_outside_bounds_is_rejected_without_saving(self):
        for search_id, interval in (("below-bound", 299), ("above-bound", 604801)):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    self._save_search(search_id, "fixture", {
                        "enabled": True, "interval_seconds": interval,
                    })
                self.assertNotIn(search_id, {row["id"] for row in list_search_profiles(self.store)})

    def test_profile_isolation_duplicate_ticks_schedule_changes_and_restart_readback(self):
        self._save_search("fixture-quick", "fixture", {"enabled": True, "interval_seconds": 600})
        self._save_search("other-slow", "other", {"enabled": True, "interval_seconds": 604800},
                          source_id=self.other_source_id)
        at = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

        schedule_due_sources(self.store, at)
        first = self._search_operations()
        self.assertEqual(len(first), 2)
        by_search = {row["params"]["search_profile_id"]: row for row in first}
        self.assertEqual(by_search["fixture-quick"]["params"]["profile_ids"], ["fixture"])
        self.assertEqual(by_search["other-slow"]["params"]["profile_ids"], ["other"])
        for search_id, expected_profile in (("fixture-quick", "fixture"), ("other-slow", "other")):
            row = by_search[search_id]
            expected_source = (self.source_id if search_id == "fixture-quick" else self.other_source_id)
            self.assertEqual(row["params"]["source_id"], expected_source)
            self.assertTrue(row["params"]["scheduler"])
            self.assertEqual(row["params"]["scheduled_for"], at.isoformat())
            event, = self._schedule_events(search_id)
            self.assertEqual(event["payload"]["profile_id"], expected_profile)
            self.assertEqual(event["payload"]["source_id"], expected_source)
            self.assertEqual(event["payload"]["scheduled_for"], at.isoformat())

        # Reopen the persistent store to simulate a scheduler process restart.
        database_url = f"sqlite:///{(self.root / 'jobs.db').as_posix()}"
        self.store.engine.dispose()
        self.store = Store(database_url)
        self.store.bind_workspace_mode("FIXTURE")
        schedule_due_sources(self.store, at)
        self.assertEqual(len(self._search_operations()), 2)
        self.assertEqual(len(self._schedule_events()), 2)
        self._complete_discoveries()

        before_due = at + timedelta(seconds=599)
        schedule_due_sources(self.store, before_due)
        self.assertEqual(len(self._search_operations()), 2)

        # Shortening the interval takes effect against the latest durable enqueue time.
        changed_at = at + timedelta(seconds=601)
        self._save_search("fixture-quick", "fixture", {
            "enabled": True, "interval_seconds": 300,
        })
        self.assertEqual(self._saved_search("fixture-quick")["criteria"]["include_titles"],
                         ["System Administrator"])
        schedule_due_sources(self.store, changed_at)
        self.assertEqual(len(self._search_operations()), 3)
        latest = self._schedule_events("fixture-quick")[-1]
        self.assertEqual(latest["payload"]["scheduled_for"], changed_at.isoformat())
        self._complete_discoveries()

        one_second_before = changed_at + timedelta(seconds=299)
        schedule_due_sources(self.store, one_second_before)
        self.assertEqual(len(self._search_operations()), 3)
        due = changed_at + timedelta(seconds=300)
        schedule_due_sources(self.store, due)
        self.assertEqual(len(self._search_operations()), 4)
        self.assertEqual(len(self._schedule_events("fixture-quick")), 3)
        self.assertEqual(len(self._schedule_events("other-slow")), 1)
        self.assertEqual(self.store.count("job_postings"), 0)


if __name__ == "__main__":
    unittest.main()
