"""Durable worker-operation progress validation and terminal-state protection."""
from pathlib import Path
import tempfile
import unittest

from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.operations.progress import get_operation_progress, record_operation_progress


class OperationProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-operation-progress-")
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'operations.db').as_posix()}")
        self.store.migrate()
        self.operation_id = "operation-progress-test"
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=self.operation_id,
                kind="DISCOVER",
                entity_id="profile-progress-test",
                params={"profile_ids": ["profile-progress-test"], "api_token": "never expose"},
                state="QUEUED",
                error=None,
                updated_at=now(),
                attempt_count=0,
                cancel_requested=False,
                lease_expires_at=None,
                last_attempt_at=None,
                dead_lettered_at=None,
                discarded_at=None,
                current_step=None,
                progress=0,
                started_at=None,
                completed_at=None,
                retry_count=0,
                recovery_metadata={},
            ))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_progress_update_persists_safe_read_model_and_stable_timestamps(self):
        result = record_operation_progress(
            self.store, self.operation_id, current_step="FETCH_SOURCE", progress=25,
            state="RUNNING", recovery_metadata={
                "action": "RETRY", "reason_code": "OPERATION_TIMEOUT", "retryable": True,
            },
        )

        self.assertEqual(set(result), {
            "operation_id", "operation_type", "entity_id", "state", "current_step", "progress",
            "started_at", "updated_at", "completed_at", "next_attempt_at", "retry_count", "recovery_summary",
        })
        self.assertEqual(result["operation_type"], "DISCOVER")
        self.assertEqual(result["state"], "RUNNING")
        self.assertEqual(result["progress"], 25)
        self.assertIsNotNone(result["started_at"])
        self.assertIsNone(result["completed_at"])
        self.assertEqual(result["recovery_summary"], "RETRY: OPERATION_TIMEOUT")
        self.assertNotIn("api_token", repr(result))
        self.assertNotIn("recovery_metadata", result)
        self.assertNotIn("params", result)
        self.assertNotIn("error", result)

        persisted = get_operation_progress(self.store, self.operation_id)
        self.assertEqual(persisted, result)

    def test_progress_cannot_regress_and_retry_count_is_monotonic(self):
        record_operation_progress(
            self.store, self.operation_id, current_step="FETCH_SOURCE", progress=50,
            state="RUNNING", retry_count=2,
        )
        before = get_operation_progress(self.store, self.operation_id)

        with self.assertRaisesRegex(ValueError, "PROGRESS_REGRESSION"):
            record_operation_progress(
                self.store, self.operation_id, current_step="CONNECT_SOURCE", progress=49,
                retry_count=2,
            )
        with self.assertRaisesRegex(ValueError, "RETRY_COUNT_REGRESSION"):
            record_operation_progress(
                self.store, self.operation_id, current_step="FETCH_SOURCE", progress=50,
                retry_count=1,
            )

        self.assertEqual(get_operation_progress(self.store, self.operation_id), before)

    def test_omitted_step_does_not_clear_the_current_step(self):
        record_operation_progress(
            self.store, self.operation_id, current_step="FETCH_SOURCE", progress=25,
            state="RUNNING",
        )

        updated = record_operation_progress(
            self.store, self.operation_id, current_step=None, progress=30,
        )

        self.assertEqual(updated["current_step"], "FETCH_SOURCE")
        self.assertEqual(updated["progress"], 30)

    def test_invalid_progress_state_transition_and_recovery_metadata_do_not_write(self):
        invalid_updates = (
            {"current_step": "FETCH", "progress": -1},
            {"current_step": "FETCH", "progress": 101},
            {"current_step": "bad value", "progress": 10},
            {"current_step": "FETCH", "progress": 10, "state": "COMPLETED"},
            {"current_step": "FETCH", "progress": 10, "state": ["RUNNING"]},
            {"current_step": "FETCH", "progress": 10,
             "recovery_metadata": {"api_token": "secret"}},
            {"current_step": "FETCH", "progress": 10,
             "recovery_metadata": {"reason_code": "raw provider said token=private"}},
            {"current_step": "FETCH", "progress": 10,
             "recovery_metadata": {"retry_after_seconds": 100000}},
        )
        for update in invalid_updates:
            with self.subTest(update=update):
                before = get_operation_progress(self.store, self.operation_id)
                args = {"retry_count": None, "recovery_metadata": None}
                args.update(update)
                with self.assertRaises(ValueError):
                    record_operation_progress(self.store, self.operation_id, **args)
                self.assertEqual(get_operation_progress(self.store, self.operation_id), before)

    def test_terminal_state_sets_completion_time_and_cannot_be_reopened_or_mutated(self):
        record_operation_progress(
            self.store, self.operation_id, current_step="FINISHED", progress=100,
            state="RUNNING",
        )
        completed = record_operation_progress(
            self.store, self.operation_id, current_step="FINISHED", progress=100,
            state="COMPLETED",
        )
        self.assertIsNotNone(completed["completed_at"])
        self.assertEqual(completed["progress"], 100)

        for state in ("RUNNING", "QUEUED", "FAILED"):
            with self.subTest(state=state):
                with self.assertRaisesRegex(ValueError, "OPERATION_TERMINAL"):
                    record_operation_progress(
                        self.store, self.operation_id, current_step="REOPENED", progress=100,
                        state=state,
                    )
                self.assertEqual(get_operation_progress(self.store, self.operation_id), completed)

    def test_retry_count_and_progress_reject_non_integer_types(self):
        for progress, retry_count in ((True, 0), (1.5, 0), (0, True), (0, -1)):
            with self.subTest(progress=progress, retry_count=retry_count):
                with self.assertRaises(ValueError):
                    record_operation_progress(
                        self.store, self.operation_id, current_step="FETCH",
                        progress=progress, retry_count=retry_count,
                    )

    def test_missing_operation_has_stable_code(self):
        with self.assertRaisesRegex(ValueError, "OPERATION_NOT_FOUND"):
            get_operation_progress(self.store, "missing-operation")
        with self.assertRaisesRegex(ValueError, "OPERATION_NOT_FOUND"):
            record_operation_progress(
                self.store, "missing-operation", current_step="FETCH", progress=1,
            )


if __name__ == "__main__":
    unittest.main()
