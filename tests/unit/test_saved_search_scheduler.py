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

    def add_search(self, search_id, source_id, interval_seconds, *, enabled=True, window=None):
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
            if window is not None:
                criteria["scheduler"]["window"] = window
            connection.execute(update(m.search_profiles).where(
                m.search_profiles.c.id == search_id).values(criteria=criteria))

    def complete_operations(self, at):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).values(state="COMPLETED"))
            connection.execute(update(m.job_sources).values(last_attempt=at.isoformat()))

    def test_persisted_unsupported_rules_cannot_bypass_admission_or_fall_back_to_source_polling(self):
        from services.discovery.search_profiles import _clean_search_scheduler

        source_id = configure_source(self.store, "invalid-rule", poll_interval=300, jitter_seconds=0)
        self.add_search("invalid-rule", source_id, 300)
        invalid = [
            {"enabled": True, "interval_seconds": 300, "kind": "AUTO_SUBMIT"},
            {"enabled": True, "interval_seconds": 300, "last_run": None},
            {"enabled": True, "interval_seconds": 300, "unsupported": "synthetic"},
            {"enabled": True}, {"interval_seconds": 300}, None, [],
            {"enabled": 1, "interval_seconds": 300},
            {"enabled": True, "interval_seconds": True},
            {"enabled": True, "interval_seconds": 300.0},
            {"enabled": True, "interval_seconds": 299},
            {"enabled": True, "interval_seconds": 604801},
            {"enabled": True, "interval_seconds": 300,
             "window": {"time_zone": "Not/A_Zone", "start_local": "08:00", "end_local": "09:00"}},
            {"enabled": True, "interval_seconds": 300,
             "window": {"time_zone": "America/Chicago", "start_local": "8:00", "end_local": "09:00"}},
            {"enabled": True, "interval_seconds": 300,
             "window": {"time_zone": "America/Chicago", "start_local": "08:00", "end_local": "08:00"}},
            {"enabled": True, "interval_seconds": 300,
             "window": {"time_zone": "America/Chicago", "start_local": "24:00", "end_local": "08:00"}},
            {"enabled": True, "interval_seconds": 300,
             "window": {"time_zone": "x" * 129, "start_local": "08:00", "end_local": "09:00"}},
            {"enabled": True, "interval_seconds": 300, "window": None},
            {"enabled": True, "interval_seconds": 300,
             "window": {"time_zone": "America/Chicago", "start_local": "08:00", "end_local": "09:00", "extra": True}},
            {"enabled": True, "interval_seconds": 300,
             "window": {"time_zone": "America/Chicago", "start_local": "08:00", "end_local": "09:00"}, "extra": True},
        ]
        for rule in invalid:
            with self.subTest(rule=rule):
                with self.assertRaises(ValueError):
                    _clean_search_scheduler(rule)
                with self.store.engine.begin() as connection:
                    criteria = dict(connection.execute(select(m.search_profiles.c.criteria).where(
                        m.search_profiles.c.id == "invalid-rule")).scalar_one())
                    criteria["scheduler"] = rule
                    connection.execute(update(m.search_profiles).where(
                        m.search_profiles.c.id == "invalid-rule").values(criteria=criteria))
                before = self.store.workspace_status()
                result = schedule_due_sources(self.store, self.at)
                self.assertEqual(result["scheduled"], [])
                self.assertEqual(result["saved_searches"]["scheduled"], [])
                self.assertEqual(self.store.workspace_status(), before,
                                 "Rejected rules must not mutate the outbox, audit or source policy")

    def test_due_tick_persists_one_idempotent_operation_and_audit_receipt(self):
        from packages.domain.automation import SavedSearchRule

        self.assertEqual(SavedSearchRule.from_mapping({
            "enabled": True, "interval_seconds": 600,
        }).as_dict(), {"enabled": True, "interval_seconds": 600})
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

    def test_saved_search_rule_path_accepts_and_persists_valid_window(self):
        from services.discovery.search_profiles import get_search_profile

        source_id = configure_source(self.store, "window-rule-path", poll_interval=300, jitter_seconds=0)
        window = {"time_zone": "America/Chicago", "start_local": "06:00", "end_local": "09:00"}
        save_search_profile(self.store, {
            "id": "window-rule-path-search", "profile_id": "fixture", "name": "Window rule path",
            "enabled": True, "include_titles": ["Systems Engineer"], "remote": "ANY",
            "employment_types": [], "source_ids": [source_id],
            "scheduler": {"enabled": True, "interval_seconds": 600, "window": window},
        })

        saved = get_search_profile(self.store, "window-rule-path-search")

        self.assertEqual(saved["criteria"]["scheduler"], {
            "enabled": True, "interval_seconds": 600, "window": window,
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

    def test_window_blocks_new_search_outside_then_schedules_inside_without_duplicate_tick(self):
        source_id = configure_source(self.store, "window-new-search", poll_interval=300, jitter_seconds=0)
        self.add_search("window-new", source_id, 300, window={
            "time_zone": "America/Chicago", "start_local": "06:00", "end_local": "09:00",
        })

        outside = schedule_due_search_profiles(self.store, self.at - timedelta(hours=2))
        inside = schedule_due_search_profiles(self.store, self.at)
        repeated = schedule_due_search_profiles(self.store, self.at)

        self.assertEqual(outside["scheduled"], [])
        self.assertTrue(any(row["reason"] == "OUTSIDE_SCHEDULE_WINDOW" for row in outside["skipped"]))
        self.assertEqual(len(inside["scheduled"]), 1)
        self.assertEqual(repeated["scheduled"], [])
        self.assertEqual(inside["scheduled"][0]["scheduled_for"], self.at.isoformat())

    def test_due_saved_search_waits_outside_window_then_reopens_at_a_later_eligible_tick(self):
        source_id = configure_source(self.store, "window-reopened-search", poll_interval=300, jitter_seconds=0)
        self.add_search("window-reopened", source_id, 300, window={
            "time_zone": "America/Chicago", "start_local": "06:00", "end_local": "09:00",
        })
        first = schedule_due_search_profiles(self.store, self.at)
        self.complete_operations(self.at)
        outside = schedule_due_search_profiles(self.store, self.at + timedelta(hours=3, minutes=30))
        next_eligible = schedule_due_search_profiles(self.store, self.at + timedelta(days=1))

        self.assertEqual(len(first["scheduled"]), 1)
        self.assertEqual(outside["scheduled"], [])
        self.assertTrue(any(row["reason"] == "OUTSIDE_SCHEDULE_WINDOW" for row in outside["skipped"]))
        self.assertEqual(len(next_eligible["scheduled"]), 1)

    def test_window_crosses_midnight_with_inclusive_start_and_exclusive_end(self):
        source_id = configure_source(self.store, "window-overnight", poll_interval=300, jitter_seconds=0)
        self.add_search("overnight", source_id, 300, window={
            "time_zone": "America/Chicago", "start_local": "22:00", "end_local": "02:00",
        })
        start = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)  # 22:00 local
        end = datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc)  # 02:00 local

        at_start = schedule_due_search_profiles(self.store, start)
        self.complete_operations(start)
        at_end = schedule_due_search_profiles(self.store, end)

        self.assertEqual(len(at_start["scheduled"]), 1)
        self.assertEqual(at_end["scheduled"], [])
        self.assertTrue(any(row["reason"] == "OUTSIDE_SCHEDULE_WINDOW" for row in at_end["skipped"]))

    def test_window_uses_local_time_across_spring_and_fall_clock_changes(self):
        from packages.domain.automation import SavedSearchRule
        from services.discovery.search_profiles import _clean_search_scheduler

        spring_rule = SavedSearchRule.from_mapping({
            "enabled": True, "interval_seconds": 300,
            "window": {"time_zone": "America/Chicago", "start_local": "03:00", "end_local": "04:00"},
        })
        fall_rule = SavedSearchRule.from_mapping({
            "enabled": True, "interval_seconds": 300,
            "window": {"time_zone": "America/Chicago", "start_local": "01:30", "end_local": "02:30"},
        })
        self.assertEqual(_clean_search_scheduler(spring_rule.as_dict()), spring_rule.as_dict())

        self.assertTrue(spring_rule.window.includes(datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc)))
        self.assertTrue(fall_rule.window.includes(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)))
        self.assertTrue(fall_rule.window.includes(datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)))

    def test_scheduler_receipts_normalize_aware_tick_to_utc(self):
        source_id = configure_source(self.store, "window-utc-receipt", poll_interval=300, jitter_seconds=0)
        self.add_search("utc-receipt", source_id, 300)
        local_tick = datetime.fromisoformat("2026-10-08T07:00:00-05:00")

        result = schedule_due_search_profiles(self.store, local_tick)

        self.assertEqual(result["scheduled"][0]["scheduled_for"], "2026-10-08T12:00:00+00:00")
        with self.store.engine.connect() as connection:
            receipt = connection.execute(select(m.audit).where(
                m.audit.c.action == "SEARCH_PROFILE_SCHEDULE_ENQUEUED")).mappings().one()
        self.assertEqual(receipt["payload"]["scheduled_for"], "2026-10-08T12:00:00+00:00")

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
