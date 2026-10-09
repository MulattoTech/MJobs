"""Map terminal fixture discovery operations to durable history records."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_history import list_search_history, record_search_history
from services.discovery.search_history_events import build_search_history_record


PROFILE = {
    "id": "fixture",
    "name": "Synthetic Profile",
    "email": "synthetic@example.test",
    "answers": [],
    "facts": [],
}


class SearchHistoryEventTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-search-history-events-")
        self.database = Path(self.directory.name) / "events.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.save_profile(dict(PROFILE))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _operation(self, **overrides):
        return {
            "id": "fixture-operation-1",
            "kind": "DISCOVER",
            "entity_id": "fixture",
            "params": {
                "source_id": "fixture-source",
                "query": "systems administrator",
                "filters": {"remote": "REQUIRED"},
            },
            "state": "COMPLETED",
            "error": None,
            "attempt_count": 1,
            "last_attempt_at": "2026-10-09T10:00:00Z",
            "updated_at": "2026-10-09T10:01:00Z",
            **overrides,
        }

    def _metadata(self, **overrides):
        return {
            "source": "fixture-source",
            "query": "systems administrator",
            "filters": {"remote": "REQUIRED"},
            "results": 4,
            "new_jobs": 2,
            "duplicates": 2,
            "errors": [],
            **overrides,
        }

    def _history_event_count(self):
        with self.store.engine.connect() as connection:
            return len(connection.execute(select(m.audit.c.id).where(
                m.audit.c.action.in_(("SEARCH_RUN_COMPLETED", "SEARCH_RUN_FAILED")),
            )).all())

    def test_completed_operation_maps_actual_source_query_filters_and_counts(self):
        record = build_search_history_record(
            self._operation(), self._metadata(source="actual-source"),
        )

        self.assertEqual(record, {
            "run_id": "fixture-operation-1:1",
            "profile_id": "fixture",
            "source": "actual-source",
            "query": "systems administrator",
            "filters": {"remote": "REQUIRED"},
            "started_at": "2026-10-09T10:00:00.000000+00:00",
            "finished_at": "2026-10-09T10:01:00.000000+00:00",
            "status": "COMPLETED",
            "results": 4,
            "new_jobs": 2,
            "duplicates": 2,
            "errors": [],
        })

    def test_dead_lettered_operation_maps_to_failed_and_preserves_error(self):
        operation = self._operation(
            state="DEAD_LETTERED", error="SOURCE_UNAVAILABLE",
            dead_lettered_at="2026-10-09T10:01:30Z",
            updated_at="2026-10-09T10:01:30Z",
        )
        metadata = self._metadata(results=1, new_jobs=0, duplicates=0)
        metadata.pop("errors")
        record = build_search_history_record(operation, metadata)

        self.assertEqual(record["status"], "FAILED")
        self.assertEqual(
            record["finished_at"], "2026-10-09T10:01:30.000000+00:00",
        )
        self.assertEqual(record["errors"], ["SOURCE_UNAVAILABLE"])

    def test_cancelled_operation_remains_cancelled_in_history(self):
        operation = self._operation(state="CANCELLED", error="CANCEL_REQUESTED")
        record = build_search_history_record(operation, self._metadata())
        self.assertEqual(record["status"], "CANCELLED")
        self.assertEqual(record["errors"], ["CANCEL_REQUESTED"])

    def test_unknown_descriptors_and_counts_remain_unknown(self):
        operation = self._operation(params={})
        record = build_search_history_record(operation, self._metadata(
            source=None, query=None, filters=None, results=None,
            new_jobs=None, duplicates=None,
        ))
        self.assertEqual(record["source"], "UNKNOWN")
        self.assertEqual(record["query"], "")
        self.assertEqual(record["filters"], {})
        self.assertIsNone(record["results"])
        self.assertIsNone(record["new_jobs"])
        self.assertIsNone(record["duplicates"])
        self.assertEqual(build_search_history_record(
            operation, self._metadata(results=0, new_jobs=0, duplicates=0),
        )["results"], 0)
        self.assertIsNone(build_search_history_record(
            self._operation(state="RUNNING"), self._metadata(),
        ))
        self.assertIsNone(build_search_history_record(
            self._operation(params={"rescore_only": True}), self._metadata(),
        ))
        self.assertEqual(self._history_event_count(), 0)

    def test_repeated_event_for_one_operation_attempt_does_not_duplicate_history(self):
        operation = self._operation()
        metadata = self._metadata()
        first = build_search_history_record(operation, metadata)
        retry = build_search_history_record(operation, metadata)
        self.assertEqual(first, retry)

        record_search_history(self.store, first)
        record_search_history(self.store, retry)

        history = list_search_history(self.store, "fixture")
        self.assertEqual(len(history["runs"]), 1)
        self.assertEqual(history["runs"][0]["run_id"], "fixture-operation-1:1")
        self.assertEqual(self._history_event_count(), 1)

    def test_unknown_counts_persist_and_replay_without_becoming_zero(self):
        record = build_search_history_record(self._operation(), self._metadata(
            results=None, new_jobs=None, duplicates=None,
        ))

        self.assertEqual(record_search_history(self.store, record), record)
        self.assertEqual(record_search_history(self.store, record), record)
        persisted = list_search_history(self.store, "fixture")["runs"][0]
        self.assertIsNone(persisted["results"])
        self.assertIsNone(persisted["new_jobs"])
        self.assertIsNone(persisted["duplicates"])
        self.assertEqual(self._history_event_count(), 1)


if __name__ == "__main__":
    unittest.main()
