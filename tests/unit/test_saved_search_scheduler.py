"""Per-saved-search cadence scheduling persists only accepted outbox work."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.scheduler import schedule_due_search_profiles, schedule_due_sources
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class SavedSearchSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-saved-search-scheduler-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'scheduler.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        self.at = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def add_search(self, search_id, source_id, interval_seconds, *, enabled=True):
        save_search_profile(self.store, {
            "id": search_id,
            "profile_id": "fixture",
            "name": search_id,
            "enabled": True,
            "include_titles": ["Systems Engineer"],
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [source_id],
        })
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.search_profiles.c.criteria).where(
                m.search_profiles.c.id == search_id)).one()
            criteria = dict(row.criteria)
            criteria["scheduler"] = {
                "enabled": enabled,
                "interval_seconds": interval_seconds,
            }
            connection.execute(update(m.search_profiles).where(
                m.search_profiles.c.id == search_id).values(criteria=criteria))

    def complete_operations(self, at):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).values(state="COMPLETED"))
            connection.execute(update(m.job_sources).values(last_attempt=at.isoformat()))

    def test_due_tick_persists_one_idempotent_operation_and_audit_receipt(self):
        source_id = configure_source(self.store, "scheduled-search", poll_interval=300, jitter_seconds=0)
        self.add_search("search-one", source_id, 600)

        first = schedule_due_sources(self.store, self.at)
        repeated = schedule_due_sources(self.store, self.at)

        self.assertEqual(first["scheduled"], [])
        self.assertEqual(len(first["saved_searches"]["scheduled"]), 1)
        scheduled = first["saved_searches"]["scheduled"][0]
        self.assertEqual(scheduled["search_profile_id"], "search-one")
        self.assertEqual(scheduled["operation_id"], identity(
            "operation-task", "DISCOVER",
            f"collect:{source_id}:saved-search:search-one:{self.at.isoformat()}",
        ))
        self.assertEqual(scheduled["source_id"], source_id)
        self.assertEqual(repeated["saved_searches"]["scheduled"], [])
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations)).mappings().one()
            receipts = list(connection.execute(select(m.audit).where(
                m.audit.c.action == "SEARCH_PROFILE_SCHEDULE_ENQUEUED")).mappings())
        self.assertEqual(operation["kind"], "DISCOVER")
        self.assertEqual(operation["entity_id"], "fixture")
        self.assertEqual(operation["params"]["profile_ids"], ["fixture"])
        self.assertEqual(operation["params"]["search_profile_id"], "search-one")
        self.assertEqual(operation["params"]["source_id"], source_id)
        self.assertEqual(operation["params"]["scheduled_for"], self.at.isoformat())
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["entity_id"], "search-one")
        self.assertEqual(receipts[0]["payload"], {
            "profile_id": "fixture", "source_id": source_id,
            "scheduled_for": self.at.isoformat(),
        })

    def test_profiles_keep_independent_intervals(self):
        fast_source = configure_source(self.store, "fast-search", poll_interval=300, jitter_seconds=0)
        slow_source = configure_source(self.store, "slow-search", poll_interval=300, jitter_seconds=0)
        self.add_search("fast", fast_source, 600)
        self.add_search("slow", slow_source, 1200)

        initial = schedule_due_search_profiles(self.store, self.at)
        self.complete_operations(self.at)
        after_ten_minutes = schedule_due_search_profiles(self.store, self.at + timedelta(minutes=10))

        self.assertEqual({row["search_profile_id"] for row in initial["scheduled"]}, {"fast", "slow"})
        self.assertEqual([row["search_profile_id"] for row in after_ten_minutes["scheduled"]], ["fast"])

    def test_shared_source_serializes_due_searches_and_serves_the_next_overdue_profile(self):
        source_id = configure_source(self.store, "shared-source", poll_interval=300, jitter_seconds=0)
        self.add_search("alpha", source_id, 600)
        self.add_search("beta", source_id, 600)

        first = schedule_due_search_profiles(self.store, self.at)
        self.complete_operations(self.at)
        second = schedule_due_search_profiles(self.store, self.at + timedelta(minutes=5))

        self.assertEqual(len(first["scheduled"]), 1)
        self.assertEqual(len(second["scheduled"]), 1)
        self.assertNotEqual(first["scheduled"][0]["search_profile_id"],
                            second["scheduled"][0]["search_profile_id"])
        self.assertTrue(any(row["reason"] == "ACTIVE_OPERATION" for row in first["skipped"]))

    def test_source_poll_interval_is_a_lower_bound_even_when_search_is_due(self):
        source_id = configure_source(self.store, "slow-source", poll_interval=900, jitter_seconds=0)
        self.add_search("fast-search", source_id, 300)
        schedule_due_search_profiles(self.store, self.at)
        self.complete_operations(self.at)

        early = schedule_due_search_profiles(self.store, self.at + timedelta(minutes=10))
        at_source_minimum = schedule_due_search_profiles(self.store, self.at + timedelta(minutes=15))

        self.assertEqual(early["scheduled"], [])
        self.assertTrue(any(row["reason"] == "SOURCE_NOT_DUE" for row in early["skipped"]))
        self.assertEqual(len(at_source_minimum["scheduled"]), 1)

    def test_disabled_search_and_active_source_operations_are_not_enqueued(self):
        disabled_source = configure_source(self.store, "disabled-search", poll_interval=300, jitter_seconds=0)
        active_source = configure_source(self.store, "active-search", poll_interval=300, jitter_seconds=0)
        self.add_search("disabled", disabled_source, 600, enabled=False)
        self.add_search("active", active_source, 600)
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id="active-discovery", kind="DISCOVER", entity_id="active",
                params={"source_id": active_source, "profile_ids": ["fixture"]},
                state="RUNNING", error=None, updated_at=self.at.isoformat(),
            ))

        result = schedule_due_sources(self.store, self.at)

        self.assertEqual(result["scheduled"], [])
        self.assertEqual(result["saved_searches"]["scheduled"], [])
        self.assertTrue(any(row["reason"] == "ACTIVE_OPERATION"
                            for row in result["saved_searches"]["skipped"]))
        with self.store.engine.connect() as connection:
            receipts = connection.execute(select(m.audit).where(
                m.audit.c.action == "SEARCH_PROFILE_SCHEDULE_ENQUEUED")).all()
        self.assertEqual(receipts, [])

    def test_failed_enqueue_does_not_write_an_audit_receipt(self):
        source_id = configure_source(self.store, "failed-enqueue", poll_interval=300, jitter_seconds=0)
        self.add_search("failed-search", source_id, 600)

        with patch("services.discovery.scheduler.enqueue_operation",
                   side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                schedule_due_search_profiles(self.store, self.at)

        with self.store.engine.connect() as connection:
            operations = list(connection.execute(select(m.operations)).mappings())
            receipts = list(connection.execute(select(m.audit).where(
                m.audit.c.action == "SEARCH_PROFILE_SCHEDULE_ENQUEUED")).mappings())
        self.assertEqual(operations, [])
        self.assertEqual(receipts, [])

    def test_audit_failure_rolls_back_the_outbox_acceptance(self):
        source_id = configure_source(self.store, "failed-audit", poll_interval=300, jitter_seconds=0)
        self.add_search("audit-failure", source_id, 600)

        with patch.object(self.store, "_audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                schedule_due_search_profiles(self.store, self.at)

        with self.store.engine.connect() as connection:
            operations = list(connection.execute(select(m.operations)).mappings())
            receipts = list(connection.execute(select(m.audit).where(
                m.audit.c.action == "SEARCH_PROFILE_SCHEDULE_ENQUEUED")).mappings())
        self.assertEqual(operations, [])
        self.assertEqual(receipts, [])


if __name__ == "__main__":
    unittest.main()
