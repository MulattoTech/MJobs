"""Commit-authoritative local cancellation must not starve its SQLite writer."""
from collections import deque
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
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
        poll_entered = threading.Event()
        writer_finished = threading.Event()
        reads_before_resolution = []
        watcher_results = []
        watcher_errors = []
        wait_results = []
        active_attempts = []
        # Bounded observations only: retain the original timer and thread order.
        phase_records = deque(maxlen=96)
        recording = False
        original_staging = operations._stage_operation_cancellation
        original_stage = local.stage
        original_wait = local.wait_for_commit
        original_audit = self.store._audit
        rollback_failure = rollback_error("SYNTHETIC_AUDIT_FAILURE")
        if prior_rollback:
            with patch.object(self.store, "_audit", side_effect=RuntimeError("SYNTHETIC_AUDIT_FAILURE")):
                with self.assertRaisesRegex(RuntimeError, "^SYNTHETIC_AUDIT_FAILURE$"):
                    operations.request_cancel(self.store, identifier)

        def record_phase(label):
            if recording:
                phase_records.append((
                    label, time.monotonic(), time.thread_time(),
                    threading.current_thread().name,
                ))

        def observed_span(label, operation):
            def observed(*args, **kwargs):
                record_phase(label + "_enter")
                try:
                    return operation(*args, **kwargs)
                finally:
                    record_phase(label + "_exit")
            return observed

        staging_with_phases = observed_span("staging_lookup", original_staging)

        def observe_staging(operation_id):
            if operation_id != identifier:
                return original_staging(operation_id)
            return staging_with_phases(operation_id)

        def sql_phase(statement):
            upper = statement.lstrip().upper()
            if upper.startswith("BEGIN IMMEDIATE"):
                return "BEGIN_IMMEDIATE"
            verb = upper.split(None, 1)[0] if upper else "EMPTY"
            table = ("operations" if m.operations.name.upper() in upper else
                     "audit" if m.audit.name.upper() in upper else "other")
            return verb + "_" + table

        def before_sql(_connection, _cursor, statement, *_args):
            record_phase("sql_" + sql_phase(statement) + "_enter")

        def after_sql(_connection, _cursor, statement, *_args):
            record_phase("sql_" + sql_phase(statement) + "_exit")

        def request_with_phases():
            record_phase("request_enter")
            try:
                return operations.request_cancel(self.store, identifier)
            finally:
                record_phase("request_exit")

        def observe_stage():
            record_phase("stage_enter")
            try:
                attempt = original_stage()
                active_attempts.append(attempt)
                return attempt
            finally:
                record_phase("stage_exit")

        def observe_wait(*args, **kwargs):
            record_phase("poll_entered")
            poll_entered.set()
            try:
                committed = original_wait(*args, **kwargs)
                wait_results.append(committed)
                return committed
            finally:
                record_phase("wait_exit")

        def observe_read(_connection, _cursor, statement, *_args):
            if (threading.current_thread().name == watcher_name
                    and statement.lstrip().upper().startswith("SELECT")
                    and f"FROM {m.operations.name}" in statement
                    and (not active_attempts or not active_attempts[-1].resolved.is_set())):
                # Keep a real SQLite cursor/read lock open while the writer
                # tries to commit. This models a descheduled polling reader.
                # The coordinator has a finite release even on old code.
                reads_before_resolution.append(True)
                record_phase("premature_read")
                poll_entered.set()
                writer_finished.wait(.5)
                record_phase("premature_read_released")

        def check():
            record_phase("watcher_check_enter")
            try:
                operations.check_cancelled(self.store, identifier, 1, local)
                watcher_results.append("RUNNING")
            except operations.OperationCancelled:
                watcher_results.append("CANCELLED")
            except BaseException as error:
                watcher_errors.append(type(error).__name__)
            finally:
                record_phase("watcher_check_exit")

        watcher = threading.Thread(target=check, name=watcher_name, daemon=True)

        def gated_audit(connection, entity_id, action, payload):
            cancellation_audit = action == "CANCELLATION_REQUESTED"
            if cancellation_audit:
                record_phase("audit_enter")
            try:
                if cancellation_audit:
                    record_phase("watcher_start_enter")
                    watcher.start()
                    record_phase("watcher_start_exit")
                    if not poll_entered.wait(2):
                        raise AssertionError(f"Owned watcher never entered its control check; errors={watcher_errors}")
                    record_phase("poll_acknowledged")
                    if rollback:
                        raise rollback_failure
                return original_audit(connection, entity_id, action, payload)
            finally:
                if cancellation_audit:
                    record_phase("audit_exit")

        # Install pass-through observers before the original measurement starts.
        # No SQL parameters or payloads are retained, and no clocks are replaced.
        diagnostics = ExitStack()
        self.addCleanup(diagnostics.close)
        diagnostics.enter_context(patch.object(
            operations, "_stage_operation_cancellation", new=observe_staging,
        ))
        for owner, attribute, label in (
                (self.store.engine, "connect", "connect"),
                (self.store.engine.dialect, "do_commit", "dbapi_commit"),
                (self.store.engine.dialect, "do_rollback", "dbapi_rollback")):
            diagnostics.enter_context(patch.object(
                owner, attribute, new=observed_span(label, getattr(owner, attribute)),
            ))
        for event_name, listener in (
                ("before_cursor_execute", before_sql),
                ("after_cursor_execute", after_sql)):
            event.listen(self.store.engine, event_name, listener)
            diagnostics.callback(event.remove, self.store.engine, event_name, listener)
        for event_name in ("checkout", "reset", "checkin"):
            listener = lambda *_args, label="pool_" + event_name: record_phase(label)
            event.listen(self.store.engine.pool, event_name, listener)
            diagnostics.callback(event.remove, self.store.engine.pool, event_name, listener)

        event.listen(self.store.engine, "after_cursor_execute", observe_read)
        started = time.monotonic()
        recording = True
        record_phase("measurement_enter")
        try:
            with patch.object(local, "wait_for_commit", side_effect=observe_wait), \
                    patch.object(local, "stage", side_effect=observe_stage), \
                    patch.object(self.store, "_audit", side_effect=gated_audit):
                record_phase("patches_ready")
                if rollback:
                    with self.assertRaisesRegex(
                            rollback_error, "^SYNTHETIC_AUDIT_FAILURE$") as raised:
                        request_with_phases()
                    self.assertIs(raised.exception, rollback_failure)
                else:
                    request_with_phases()
                self.assertTrue(
                    active_attempts[-1].resolved.is_set(),
                    "Local cancellation outcome was not resolved after transaction exit",
                )
                self.assertEqual(active_attempts[-1].committed, not rollback)
                elapsed = time.monotonic() - started
                record_phase("measurement_exit")
                writer_finished.set()
                watcher.join(2)
        finally:
            writer_finished.set()
            if watcher.ident is not None:
                watcher.join(2)
            event.remove(self.store.engine, "after_cursor_execute", observe_read)
            operations.unregister_operation_cancellation(identifier, local)
            recording = False
            diagnostics.close()

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
        cpu_origins = {}
        phase_details = "; ".join(
            f"{label}@{thread_name}={wall - started:.6f}s/"
            f"cpu={cpu - cpu_origins.setdefault(thread_name, cpu):.6f}s"
            for label, wall, cpu, thread_name in phase_records
        )
        if len(phase_records) == phase_records.maxlen:
            phase_details += "; trace_capacity_reached=True"
        print(f"cancel-commit {kind}/{watcher_name}: rollback={rollback} "
              f"store={elapsed:.3f}s premature_reads={len(reads_before_resolution)} "
              f"phases=[{phase_details}]")
        self.assertEqual(
            reads_before_resolution, [],
            f"phases=[{phase_details}]; "
            "Local polling acquired a read lock during its cancellation transaction",
        )
        if not rollback:
            self.assertLess(
                elapsed, .35,
                f"Local polling delayed durable cancellation; phases=[{phase_details}]",
            )
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
        # A non-watcher name takes the real SQL path and must trip the read-lock
        # trap. Require that specific failure, not merely a slow request.
        watcher_name = "mjobs-synthetic-execution"
        with self.assertRaisesRegex(
                AssertionError,
                "Local polling acquired a read lock during its cancellation transaction$",
        ) as raised:
            self.exercise_pending_commit("DISCOVER", watcher_name)
        self.assertEqual(self.store.engine.pool.checkedout(), 0)

        message = str(raised.exception)
        writer_name = threading.current_thread().name
        phases = {}
        for label, thread_name in (
                ("premature_read", watcher_name),
                ("premature_read_released", watcher_name),
                ("dbapi_commit_enter", writer_name),
                ("dbapi_commit_exit", writer_name)):
            matches = re.findall(
                re.escape(f"{label}@{thread_name}=") + r"(-?\d+\.\d+)s/cpu=",
                message,
            )
            self.assertEqual(len(matches), 1, f"Missing or repeated {label}: {message}")
            phases[label] = float(matches[0])

        # Prove overlap with the held reader from actual DBAPI observations;
        # no additional duration threshold or clock/SQL replacement is needed.
        self.assertLessEqual(phases["premature_read"], phases["dbapi_commit_enter"])
        self.assertLess(phases["dbapi_commit_enter"], phases["premature_read_released"])
        self.assertLessEqual(phases["premature_read_released"], phases["dbapi_commit_exit"])

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
