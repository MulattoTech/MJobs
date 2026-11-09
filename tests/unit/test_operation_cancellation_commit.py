"""Commit-authoritative local cancellation must not starve its SQLite writer."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, select

from packages.persistence import models as m
from packages.persistence.store import Store, now
from workers import operations


SOURCE_WATCHERS = (
    "mjobs-greenhouse-cancellation",
    "mjobs-remotive-cancellation",
    "mjobs-remoteok-cancellation",
    "mjobs-wwr-cancellation",
)
SOURCE_KINDS = ("DISCOVER", "COMPANY_DISCOVER", "COMPANY_SOURCE_REFRESH")


class OperationCancellationCommitTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="mjobs-cancel-commit-")
        self.addCleanup(root.cleanup)
        self.store = Store(f"sqlite:///{(Path(root.name) / 'control.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")

    def seed(self, kind="DISCOVER", state="RUNNING"):
        identifier = "synthetic-" + uuid4().hex
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=identifier, kind=kind, entity_id="synthetic", params={},
                state=state, attempt_count=1, updated_at=now(),
                lease_expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            ))
        return identifier

    def exercise_pending_commit(self, kind, watcher_name, *, rollback=False,
                                prior_rollback=False, rollback_error=RuntimeError):
        identifier = self.seed(kind)
        local = operations.register_operation_cancellation(identifier)
        watcher_ready = threading.Event()
        watcher_release = threading.Event()
        abort_watcher = threading.Event()
        poll_entered = threading.Event()
        writer_finished = threading.Event()
        phase_times = {}
        reads_before_resolution = []
        watcher_results = []
        watcher_errors = []
        wait_results = []
        active_attempts = []
        original_stage = local.stage
        original_wait = local.wait_for_commit
        original_audit = self.store._audit
        rollback_failure = rollback_error("SYNTHETIC_AUDIT_FAILURE")

        def observe_stage():
            attempt = original_stage()
            active_attempts.append(attempt)
            phase_times["staged"] = time.monotonic()
            return attempt

        def observe_wait(*args, **kwargs):
            phase_times["poll_entered"] = time.monotonic()
            poll_entered.set()
            committed = original_wait(*args, **kwargs)
            wait_results.append(committed)
            phase_times["wait_finished"] = time.monotonic()
            return committed

        def observe_read(_connection, _cursor, statement, *_args):
            if (threading.current_thread().name == watcher_name
                    and statement.lstrip().upper().startswith("SELECT")
                    and f"FROM {m.operations.name}" in statement
                    and (not active_attempts or not active_attempts[-1].resolved.is_set())):
                # Keep a real SQLite cursor/read lock open while the writer
                # tries to commit. This models a descheduled polling reader.
                # The coordinator has a finite release even on old code.
                reads_before_resolution.append(True)
                phase_times["premature_read"] = time.monotonic()
                poll_entered.set()
                writer_finished.wait(.5)

        def check():
            try:
                phase_times["watcher_ready"] = time.monotonic()
                watcher_ready.set()
                if not watcher_release.wait(2):
                    raise AssertionError("Owned watcher was never released")
                if abort_watcher.is_set():
                    return
                phase_times["watcher_check"] = time.monotonic()
                operations.check_cancelled(self.store, identifier, 1, local)
                watcher_results.append("RUNNING")
            except operations.OperationCancelled:
                watcher_results.append("CANCELLED")
            except BaseException as error:
                watcher_errors.append(type(error).__name__)

        watcher = threading.Thread(target=check, name=watcher_name, daemon=True)

        def gated_audit(connection, entity_id, action, payload):
            try:
                if action == "CANCELLATION_REQUESTED":
                    phase_times["audit_entered"] = time.monotonic()
                    watcher_release.set()
                    if not poll_entered.wait(2):
                        raise AssertionError(f"Owned watcher never entered its control check; errors={watcher_errors}")
                    phase_times["poll_acknowledged"] = time.monotonic()
                    if rollback:
                        raise rollback_failure
                return original_audit(connection, entity_id, action, payload)
            finally:
                if action == "CANCELLATION_REQUESTED":
                    phase_times["audit_finished"] = time.monotonic()

        event.listen(self.store.engine, "after_cursor_execute", observe_read)
        try:
            if prior_rollback:
                with patch.object(self.store, "_audit", side_effect=RuntimeError("SYNTHETIC_AUDIT_FAILURE")):
                    with self.assertRaisesRegex(RuntimeError, "^SYNTHETIC_AUDIT_FAILURE$"):
                        operations.request_cancel(self.store, identifier)
            with patch.object(local, "wait_for_commit", side_effect=observe_wait), \
                    patch.object(local, "stage", side_effect=observe_stage), \
                    patch.object(self.store, "_audit", side_effect=gated_audit):
                # Thread creation belongs to the fixture. The staged audit
                # releases this ready observer during the real transaction.
                watcher.start()
                self.assertTrue(watcher_ready.wait(2), "Owned watcher did not become ready")
                started = time.monotonic()
                if rollback:
                    with self.assertRaisesRegex(
                            rollback_error, "^SYNTHETIC_AUDIT_FAILURE$") as raised:
                        operations.request_cancel(self.store, identifier)
                    self.assertIs(raised.exception, rollback_failure)
                else:
                    operations.request_cancel(self.store, identifier)
                phase_times["request_finished"] = time.monotonic()
                self.assertTrue(
                    active_attempts[-1].resolved.is_set(),
                    "Local cancellation outcome was not resolved after transaction exit",
                )
                self.assertEqual(active_attempts[-1].committed, not rollback)
                elapsed = time.monotonic() - started
                writer_finished.set()
                watcher.join(2)
        finally:
            abort_watcher.set()
            watcher_release.set()
            writer_finished.set()
            if watcher.ident is not None:
                watcher.join(2)
            event.remove(self.store.engine, "after_cursor_execute", observe_read)
            operations.unregister_operation_cancellation(identifier, local)

        self.assertFalse(watcher.is_alive(), "Owned watcher did not stop")
        self.assertEqual(watcher_errors, [])
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == identifier)).mappings().one()
            audits = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == identifier,
                m.audit.c.action == "CANCELLATION_REQUESTED")).all()
        self.assertEqual(row["cancel_requested"], not rollback)
        self.assertEqual(row["state"], "RUNNING")
        self.assertEqual(len(audits), 0 if rollback else 1)
        phase_details = ", ".join(
            f"{phase}={phase_times[phase] - started:.3f}s"
            for phase in (
                "watcher_ready", "staged", "audit_entered", "watcher_check",
                "poll_entered", "premature_read", "poll_acknowledged",
                "audit_finished", "request_finished", "wait_finished",
            )
            if phase in phase_times
        )
        print(f"cancel-commit {kind}/{watcher_name}: rollback={rollback} "
              f"store={elapsed:.3f}s premature_reads={len(reads_before_resolution)} "
              f"phases=[{phase_details}]")
        self.assertEqual(reads_before_resolution, [],
                         "Local polling acquired a read lock during its cancellation transaction")
        if not rollback:
            self.assertLess(elapsed, .35,
                            f"Local polling delayed durable cancellation; phases=[{phase_details}]")
        self.assertEqual(wait_results, [not rollback],
                         "Watcher did not await this cancellation attempt's outcome")
        self.assertEqual(watcher_results, ["RUNNING" if rollback else "CANCELLED"])
        self.assertEqual(self.store.engine.pool.checkedout(), 0)

    def test_source_watchers_do_not_delay_their_cancellation_commit(self):
        for kind in SOURCE_KINDS:
            for watcher in SOURCE_WATCHERS:
                with self.subTest(kind=kind, watcher=watcher):
                    self.exercise_pending_commit(kind, watcher)

    def test_execution_thread_negative_control_detects_a_premature_sqlite_read(self):
        # This name takes the real execution-thread SQL path, so the fixture
        # must detect its reader holding up the staged cancellation writer.
        with self.assertRaisesRegex(
                AssertionError,
                "Local polling acquired a read lock during its cancellation transaction$"):
            self.exercise_pending_commit("DISCOVER", "mjobs-synthetic-execution")
        self.assertEqual(self.store.engine.pool.checkedout(), 0)

    def test_source_watchers_observe_rollback_without_false_cancellation(self):
        for watcher in SOURCE_WATCHERS:
            with self.subTest(watcher=watcher):
                self.exercise_pending_commit("DISCOVER", watcher, rollback=True)

    def test_watchers_observe_process_control_rollback(self):
        for failure in (KeyboardInterrupt, SystemExit):
            for kind, watcher in (
                    ("DISCOVER", "mjobs-remoteok-cancellation"),
                    ("AI_EVALUATE_QUEUED", "mjobs-request-deadline")):
                with self.subTest(failure=failure.__name__, kind=kind):
                    self.exercise_pending_commit(
                        kind, watcher, rollback=True, rollback_error=failure,
                    )

    def test_existing_ai_watcher_preserves_commit_and_rollback_behavior(self):
        for rollback in (False, True):
            with self.subTest(rollback=rollback):
                self.exercise_pending_commit(
                    "AI_EVALUATE_QUEUED", "mjobs-request-deadline", rollback=rollback,
                )

    def test_source_watcher_waits_for_a_retry_after_failed_cancellation(self):
        self.exercise_pending_commit("DISCOVER", "mjobs-remoteok-cancellation", prior_rollback=True)

    def test_execution_thread_still_reads_durable_state_after_local_commit(self):
        identifier = self.seed()
        local = operations.register_operation_cancellation(identifier)
        try:
            attempt = local.stage()
            attempt.resolve(True)
            with patch.object(operations, "_read_operation_control",
                              wraps=operations._read_operation_control) as read:
                operations.check_cancelled(self.store, identifier, 1, local)
            read.assert_called_once_with(self.store, identifier)
        finally:
            operations.unregister_operation_cancellation(identifier, local)

    def test_queued_and_terminal_operations_do_not_stage_running_wakeup(self):
        for state in ("QUEUED", "COMPLETED"):
            with self.subTest(state=state):
                identifier = self.seed(state=state)
                local = operations.register_operation_cancellation(identifier)
                try:
                    with patch.object(local, "stage", wraps=local.stage) as stage:
                        result = operations.request_cancel(self.store, identifier)
                    stage.assert_not_called()
                    self.assertEqual(result["state"], "CANCELLED" if state == "QUEUED" else state)
                finally:
                    operations.unregister_operation_cancellation(identifier, local)

    def test_old_attempt_cannot_resolve_a_newer_pending_attempt(self):
        local = operations._LocalCancellation()
        old_attempt = local.stage()
        new_attempt = local.stage()
        old_attempt.resolve(True)
        self.assertFalse(local.wait_for_commit(timeout=0))
        new_attempt.resolve(False)
        self.assertFalse(local.wait_for_commit(timeout=0))

    def test_old_rollback_cannot_undo_a_newer_committed_attempt(self):
        local = operations._LocalCancellation()
        old_attempt = local.stage()
        new_attempt = local.stage()
        new_attempt.resolve(True)
        old_attempt.resolve(False)
        self.assertTrue(local.wait_for_commit(timeout=0))
