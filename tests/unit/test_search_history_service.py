"""Durable, profile-scoped discovery search history service."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_history import (
    list_search_history,
    record_search_history,
)


PROFILE = {
    "id": "fixture",
    "name": "Synthetic Profile",
    "email": "synthetic@example.test",
    "answers": [],
    "facts": [],
}


class SearchHistoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-search-history-")
        database = Path(self.directory.name) / "history.db"
        self.store = Store(f"sqlite:///{database.as_posix()}")
        self.store.migrate()
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other"})

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _run(self, **overrides):
        return {
            "run_id": "run-1",
            "profile_id": "fixture",
            "source": "fixture-source",
            "query": "systems administrator",
            "filters": {"remote": "REQUIRED", "include_titles": ["System Administrator"]},
            "started_at": "2026-10-09T10:00:00Z",
            "finished_at": "2026-10-09T10:01:00Z",
            "status": "COMPLETED",
            "results": 4,
            "new_jobs": 2,
            "duplicates": 2,
            "errors": [],
            **overrides,
        }

    def _audit_rows(self):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(m.audit).where(m.audit.c.action.like("SEARCH_RUN_%"))
                .order_by(m.audit.c.id)
            ).mappings()]

    def test_completed_and_failed_runs_read_back_with_profile_scope_and_cursor(self):
        completed = record_search_history(self.store, self._run())
        failed = record_search_history(self.store, self._run(
            run_id="run-2",
            source="another-fixture-source",
            query="network administrator",
            filters={"remote": "ANY"},
            started_at="2026-10-09T10:02:00Z",
            finished_at="2026-10-09T10:03:00Z",
            status="FAILED",
            results=1,
            new_jobs=0,
            duplicates=0,
            errors=["fixture source returned an unavailable response"],
        ))

        first_page = list_search_history(self.store, "fixture", limit=1)
        self.assertEqual(first_page["profile_id"], "fixture")
        self.assertEqual(first_page["runs"], [failed])
        self.assertIsInstance(first_page["next_cursor"], str)
        second_page = list_search_history(
            self.store, "fixture", limit=1, cursor=first_page["next_cursor"],
        )
        self.assertEqual(second_page, {
            "profile_id": "fixture", "runs": [completed], "next_cursor": None,
        })
        self.assertEqual(list_search_history(self.store, "other"), {
            "profile_id": "other", "runs": [], "next_cursor": None,
        })
        with self.assertRaisesRegex(ValueError, "SEARCH_HISTORY_CURSOR_INVALID"):
            list_search_history(
                self.store, "other", limit=1, cursor=first_page["next_cursor"],
            )
        self.assertEqual(len(self._audit_rows()), 2)

        database_url = self.store.url
        self.store.engine.dispose()
        reopened = Store(database_url)
        try:
            self.assertEqual(
                list_search_history(reopened, "fixture", limit=1)["runs"],
                [failed],
            )
        finally:
            reopened.engine.dispose()

    def test_exact_replay_is_idempotent_and_changed_replay_preserves_history(self):
        recorded = record_search_history(self.store, self._run())
        before = self._audit_rows()

        replay = record_search_history(self.store, self._run())
        self.assertEqual(replay, recorded)
        self.assertEqual(self._audit_rows(), before)

        with self.assertRaisesRegex(ValueError, "SEARCH_HISTORY_RUN_CONFLICT"):
            record_search_history(self.store, self._run(query="different query"))
        self.assertEqual(self._audit_rows(), before)

    def test_optional_saved_search_identity_round_trips_and_replay_is_stable(self):
        recorded = record_search_history(
            self.store, self._run(search_profile_id="remote-it-ok"),
        )

        self.assertEqual(recorded["search_profile_id"], "remote-it-ok")
        self.assertEqual(
            list_search_history(self.store, "fixture")["runs"], [recorded],
        )
        self.assertEqual(
            record_search_history(self.store, dict(recorded)), recorded,
        )
        before = self._audit_rows()
        with self.assertRaisesRegex(ValueError, "SEARCH_HISTORY_SEARCH_PROFILE_ID_INVALID"):
            record_search_history(self.store, self._run(search_profile_id="x" * 129))
        self.assertEqual(self._audit_rows(), before)

    def test_saved_search_filter_finds_its_latest_receipt_before_global_limit(self):
        older_match = record_search_history(self.store, self._run(
            run_id="search-old", search_profile_id="fixture-search",
            finished_at="2026-10-09T10:01:00Z",
        ))
        newest_other = record_search_history(self.store, self._run(
            run_id="other-new", finished_at="2026-10-09T10:06:00Z",
        ))
        latest_match = record_search_history(self.store, self._run(
            run_id="search-latest", search_profile_id="fixture-search",
            started_at="2026-10-09T10:04:00Z",
            finished_at="2026-10-09T10:05:00Z", status="FAILED",
        ))

        self.assertEqual(list_search_history(self.store, "fixture", limit=1)["runs"], [newest_other])
        matching_page = list_search_history(
            self.store, "fixture", limit=1, search_profile_id="fixture-search",
        )
        self.assertEqual(matching_page["profile_id"], "fixture")
        self.assertEqual(matching_page["runs"], [latest_match])
        self.assertIsInstance(matching_page["next_cursor"], str)
        matching_cursor = matching_page["next_cursor"]
        next_page = list_search_history(
            self.store, "fixture", limit=1, cursor=matching_cursor, search_profile_id="fixture-search",
        )
        self.assertEqual([row["run_id"] for row in next_page["runs"]], ["search-old"])
        for other_scope in (None, "another-search"):
            with self.subTest(other_scope=other_scope), self.assertRaisesRegex(ValueError, "CURSOR_INVALID"):
                list_search_history(
                    self.store, "fixture", limit=1, cursor=matching_cursor, search_profile_id=other_scope,
                )
        self.assertEqual(list_search_history(
            self.store, "other", limit=1, search_profile_id="fixture-search",
        )["runs"], [])
        self.assertEqual(older_match["search_profile_id"], "fixture-search")

    def test_invalid_cursor_and_unknown_profile_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "SEARCH_HISTORY_CURSOR_INVALID"):
            list_search_history(self.store, "fixture", cursor="not-a-cursor")
        with self.assertRaises(NoResultFound):
            list_search_history(self.store, "missing-profile")

    def test_invalid_record_is_rejected_without_appending_a_partial_run(self):
        with self.assertRaisesRegex(ValueError, "SEARCH_HISTORY_STATUS_INVALID"):
            record_search_history(self.store, self._run(status=[]))
        with self.assertRaisesRegex(ValueError, "SEARCH_HISTORY_FILTERS_INVALID"):
            record_search_history(self.store, self._run(filters={"api_token": "secret"}))
        self.assertEqual(self._audit_rows(), [])

    def test_broad_saved_search_can_record_an_empty_text_query(self):
        run = record_search_history(self.store, self._run(
            run_id="broad-search",
            query="",
            filters={"include_titles": ["Systems Administrator"]},
        ))

        self.assertEqual(run["query"], "")
        self.assertEqual(list_search_history(self.store, "fixture")["runs"], [run])

    def test_unknown_counters_are_null_and_measured_zero_stays_zero(self):
        run = record_search_history(self.store, self._run(
            run_id="unknown-counts", results=None, new_jobs=None, duplicates=None,
        ))
        zero_run = record_search_history(self.store, self._run(
            run_id="measured-zero", results=0, new_jobs=0, duplicates=0,
        ))

        self.assertIsNone(run["results"])
        self.assertIsNone(run["new_jobs"])
        self.assertIsNone(run["duplicates"])
        self.assertEqual(zero_run["results"], 0)
        self.assertEqual(zero_run["new_jobs"], 0)
        self.assertEqual(zero_run["duplicates"], 0)
        self.assertEqual(list_search_history(self.store, "fixture")["runs"], [
            zero_run, run,
        ])

        replay = record_search_history(self.store, self._run(
            run_id="unknown-counts", results=None, new_jobs=None, duplicates=None,
        ))
        self.assertEqual(replay, run)
        self.assertEqual(len(self._audit_rows()), 2)

    def test_history_write_can_share_operation_completion_transaction(self):
        run = self._run(run_id="transactional")
        with self.assertRaisesRegex(RuntimeError, "rollback sentinel"):
            with self.store.engine.begin() as connection:
                record_search_history(self.store, run, connection=connection)
                raise RuntimeError("rollback sentinel")
        self.assertEqual(list_search_history(self.store, "fixture")["runs"], [])

    def test_saved_work_authorization_filter_is_not_treated_as_a_secret(self):
        run = record_search_history(self.store, self._run(
            run_id="work-authorization-filter",
            filters={"excluded_work_authorization_flags": ["requires_sponsorship"]},
        ))
        self.assertEqual(run["filters"], {
            "excluded_work_authorization_flags": ["requires_sponsorship"],
        })

    def test_cancelled_runs_are_queryable_with_their_reason(self):
        run = record_search_history(self.store, self._run(
            run_id="cancelled", status="CANCELLED", errors=["CANCEL_REQUESTED"],
        ))
        self.assertEqual(list_search_history(self.store, "fixture")["runs"], [run])

    def test_invalid_known_counter_types_still_fail_closed(self):
        for invalid in (True, -1, 1.0, "1"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "SEARCH_HISTORY_RESULTS_INVALID"):
                    record_search_history(self.store, self._run(results=invalid))
        self.assertEqual(self._audit_rows(), [])


if __name__ == "__main__":
    unittest.main()
