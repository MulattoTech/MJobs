"""Worker crash recovery preserves already committed controlled verification.

Receipts here are synthetic persistence fixtures, not browser acceptance proof.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from workers.dispatch import enqueue_operation
from workers.operations import (
    OperationCancelled, OperationTimedOut, check_cancelled, claim, recover_expired,
    request_cancel,
)
from workers.tasks import execute_operation
from workers.structured_logging import log_operation_finished
from tests.integration import test_controlled_application_dry_run as controlled_contract


class DryRunReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-dry-run-recovery-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'test.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.environment = patch.dict(os.environ, {
            "MJOBS_OPERATION_TIMEOUT": "180", "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_DATABASE_URL": self.store.url, "MJOBS_OUTPUT_DIR": self.directory.name,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.params = {
            "application_id": "synthetic-application", "profile_id": "synthetic-profile",
            "request_id": "synthetic-run", "fingerprint": "a" * 64,
            "context": "CONTROLLED_FIXTURE", "template_id": "synthetic-template",
            "template_sha256": "b" * 64,
        }
        self.operation_id = enqueue_operation(
            self.store, "APPLICATION_DRY_RUN", self.params["application_id"], self.params)
        self.receipt = {
            key: self.params[key] for key in (
                "application_id", "profile_id", "fingerprint", "context",
                "template_id", "template_sha256",
            )
        }
        self.receipt.update(
            operation_id=self.operation_id, verification="BROWSER_SELECTED_BYTES",
            documents=[], pages=[], provider_acceptance_claimed=False,
            submission_enabled=False,
        )

    def row(self, store=None):
        with (store or self.store).engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.operation_id)).mappings().one())

    def expire(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(
                    lease_expires_at="2000-01-01T00:00:00+00:00"))

    def last_attempt(self):
        for _ in range(2):
            self.assertIsNotNone(claim(self.store, self.operation_id))
            self.expire()
            self.assertEqual(recover_expired(self.store), 1)
        operation = claim(self.store, self.operation_id)
        self.assertEqual(operation["attempt_count"], 3)
        return operation

    def save_receipt(self, *, payload=None, **overrides):
        values = {
            "id": identity("APPLICATION_DRY_RUN", self.operation_id),
            "entity_id": self.params["application_id"], "action": "APPLICATION_DRY_RUN",
            "timestamp": now(), "payload": self.receipt if payload is None else payload,
        }
        values.update(overrides)
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(**values))

    def test_final_attempt_receipt_completes_without_replay_and_survives_reopen(self):
        owner = self.last_attempt()
        self.save_receipt()
        self.expire()
        self.assertEqual(recover_expired(self.store), 1)
        self.assertEqual(recover_expired(self.store), 0)
        reopened = Store(self.store.url)
        try:
            saved = self.row(reopened)
            self.assertEqual((saved["state"], saved["error"]), ("COMPLETED", None))
            self.assertEqual(saved["attempt_count"], 3)
            self.assertEqual(saved["retry_count"], 2)
            self.assertEqual(saved["progress"], 100)
            self.assertEqual(saved["current_step"], "COMPLETED")
            self.assertEqual(saved["recovery_metadata"], {"action": "NONE"})
            self.assertIsNotNone(saved["completed_at"])
            self.assertIsNone(saved["lease_expires_at"])
            self.assertIsNone(saved["dead_lettered_at"])
            self.assertIsNone(claim(reopened, self.operation_id))
            with self.assertRaises(OperationCancelled):
                check_cancelled(reopened, self.operation_id, owner["attempt_count"])
            with reopened.engine.connect() as connection:
                self.assertEqual(connection.execute(select(m.audit.c.payload).where(
                    m.audit.c.id == identity("APPLICATION_DRY_RUN", self.operation_id)
                )).scalar_one(), self.receipt)
                events = connection.execute(select(m.audit.c.payload).where(
                    m.audit.c.entity_id == self.operation_id,
                    m.audit.c.action == "LEASE_RECOVERY")).scalars().all()
            self.assertEqual(events[-1]["state"], "COMPLETED")
        finally:
            reopened.engine.dispose()

    def test_missing_receipt_still_exhausts_retry_budget(self):
        self.last_attempt()
        self.expire()
        recover_expired(self.store)
        self.assertEqual((self.row()["state"], self.row()["error"]),
                         ("DEAD_LETTERED", "RETRY_LIMIT"))

    def test_committed_receipt_wins_over_late_cancel_flag(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(cancel_requested=True))
        self.expire()
        recover_expired(self.store)
        saved = self.row()
        self.assertEqual(saved["state"], "COMPLETED")
        self.assertFalse(saved["cancel_requested"])
        self.assertEqual(saved["retry_count"], 0)

    def test_live_owner_is_not_finalized_by_receipt_probe(self):
        owner = claim(self.store, self.operation_id)
        self.save_receipt()
        self.assertEqual(recover_expired(self.store), 0)
        self.assertEqual(self.row()["state"], "RUNNING")
        check_cancelled(self.store, self.operation_id, owner["attempt_count"])

    def test_receipt_recovery_emits_one_terminal_event_and_duplicate_work_is_silent(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        self.expire()
        with self.assertLogs("mjobs.operations", level="INFO") as captured:
            self.assertEqual(recover_expired(self.store), 1)
        self.assertEqual(len(captured.records), 1)
        record = json.loads(captured.records[0].getMessage())
        self.assertEqual(record["event"], "operation_finished")
        self.assertEqual(record["operation_id"], self.operation_id)
        self.assertEqual(record["operation_kind"], "APPLICATION_DRY_RUN")
        self.assertEqual(record["result"], "COMPLETED")
        self.assertGreaterEqual(record["duration_ms"], 0)
        with self.assertNoLogs("mjobs.operations", level="INFO"):
            self.assertEqual(recover_expired(self.store), 0)
            self.assertEqual(execute_operation(self.operation_id)["status"],
                             "ALREADY_CLAIMED_OR_CAPACITY")

    def test_recovery_terminal_event_observes_committed_state_from_fresh_store(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        self.expire()

        def verify_commit(operation, result, error, duration):
            reopened = Store(self.store.url)
            try:
                self.assertEqual(self.row(reopened)["state"], "COMPLETED")
                self.assertEqual(self.row(reopened)["recovery_metadata"], {"action": "NONE"})
            finally:
                reopened.engine.dispose()
            log_operation_finished(operation, result, error, duration)

        with patch("workers.operations.log_operation_finished", side_effect=verify_commit) as emit:
            with self.assertLogs("mjobs.operations", level="INFO"):
                recover_expired(self.store)
        emit.assert_called_once()

    def test_rolled_back_recovery_emits_nothing_and_later_commit_logs_once(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        self.expire()
        with patch.object(self.store, "_audit", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertNoLogs("mjobs.operations", level="INFO"):
                with self.assertRaisesRegex(RuntimeError, "synthetic audit failure"):
                    recover_expired(self.store)
        self.assertEqual(self.row()["state"], "RUNNING")
        with self.assertLogs("mjobs.operations", level="INFO") as captured:
            recover_expired(self.store)
        self.assertEqual(len(captured.records), 1)

    def test_recovery_context_failure_preserves_result_and_omits_private_diagnostics(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        self.expire()
        with patch("workers.operations.resolve_operation_log_context",
                   side_effect=RuntimeError("PRIVATE_RECOVERY_CONTEXT_DIAGNOSTIC")):
            with self.assertLogs("mjobs.operations", level="INFO") as captured:
                recover_expired(self.store)
        self.assertEqual(self.row()["state"], "COMPLETED")
        self.assertEqual(len(captured.records), 1)
        encoded = captured.records[0].getMessage()
        self.assertNotIn("PRIVATE_RECOVERY_CONTEXT_DIAGNOSTIC", encoded)
        record = json.loads(encoded)
        self.assertEqual(record["result"], "COMPLETED")
        self.assertNotIn("application_id", record)
        self.assertNotIn("params", record)

    def test_logging_failure_cannot_reopen_committed_recovery_or_block_dispatch(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        self.expire()
        with patch("workers.operations.log_operation_finished",
                   side_effect=RuntimeError("PRIVATE_LOG_SINK_FAILURE")) as emit:
            self.assertEqual(recover_expired(self.store), 1)
            self.assertEqual(self.row()["state"], "COMPLETED")
            self.assertEqual(recover_expired(self.store), 0)
            self.assertEqual(execute_operation(self.operation_id)["status"],
                             "ALREADY_CLAIMED_OR_CAPACITY")
        emit.assert_called_once()

    def test_cancelled_crashed_worker_without_receipt_remains_cancelled(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        request_cancel(self.store, self.operation_id)
        self.expire()
        recover_expired(self.store)
        self.assertEqual(self.row()["state"], "CANCELLED")
        self.assertIsNone(claim(self.store, self.operation_id))

    def test_worker_finalization_preserves_saved_verification_after_late_timeout(self):
        def save_then_timeout(*_args, **_kwargs):
            self.save_receipt()
            raise OperationTimedOut("synthetic post-commit timeout")

        with patch("workers.tasks.execute_controlled_dry_run", side_effect=save_then_timeout):
            result = execute_operation(self.operation_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.row()["recovery_metadata"], {"action": "NONE"})

    def test_worker_finalization_cannot_acknowledge_after_lease_expires(self):
        def save_then_expire(*_args, **_kwargs):
            self.save_receipt()
            self.expire()
            return self.receipt

        with patch("workers.tasks.execute_controlled_dry_run", side_effect=save_then_expire):
            result = execute_operation(self.operation_id)
        self.assertEqual(result["status"], "OWNERSHIP_EXPIRED")
        self.assertEqual(self.row()["state"], "RUNNING")
        recover_expired(self.store)
        self.assertEqual(self.row()["state"], "COMPLETED")

    def test_cancellation_ignores_unbound_receipt_and_preserves_valid_verification(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt(payload={**self.receipt, "fingerprint": "wrong"})
        self.assertTrue(request_cancel(self.store, self.operation_id)["cancel_requested"])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(cancel_requested=False))
            connection.execute(update(m.audit).where(
                m.audit.c.id == identity("APPLICATION_DRY_RUN", self.operation_id)
            ).values(payload=self.receipt))
        result = request_cancel(self.store, self.operation_id)
        self.assertFalse(result["cancel_requested"])
        self.assertEqual(result["reason"], "VERIFICATION_ALREADY_SAVED")

    def test_unbound_receipt_never_authorizes_completion(self):
        self.last_attempt()
        self.expire()
        invalid = [
            {"id": "unrelated-receipt"}, {"entity_id": "other-application"},
            {"action": "UNRELATED_EVENT"},
        ]
        for key, value in (
            ("operation_id", "other-operation"), ("application_id", "other-application"),
            ("profile_id", "other-profile"), ("fingerprint", "c" * 64),
            ("context", "PUBLIC"), ("template_id", "other-template"),
            ("template_sha256", "d" * 64), ("verification", "UNVERIFIED"),
            ("provider_acceptance_claimed", True), ("submission_enabled", True),
            ("submission_enabled", 0),
        ):
            invalid.append({"payload": {**self.receipt, key: value}})
        invalid.extend({"payload": payload} for payload in ([], "invalid", {}))
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.store.engine.begin() as connection:
                    connection.execute(m.audit.delete().where(
                        m.audit.c.action.not_in(("OPERATION_ATTEMPT_STARTED", "LEASE_RECOVERY"))))
                    connection.execute(update(m.operations).where(
                        m.operations.c.id == self.operation_id).values(
                            state="RUNNING", error=None, dead_lettered_at=None))
                self.save_receipt(**deepcopy(overrides))
                recover_expired(self.store)
                self.assertEqual(self.row()["state"], "DEAD_LETTERED")


class PublicDryRunReceiptCrashTests(unittest.TestCase):
    """Public admission, actual receipt transaction, hard exit and fresh recovery.

    Only the browser renderer is synthetic. No Chromium, Redis, employer HTTP,
    or real submission is used; this does not certify physical browser upload.
    """
    _new_store = controlled_contract.ControlledApplicationDryRunTests._new_store
    setUp = controlled_contract.ControlledApplicationDryRunTests.setUp
    tearDown = controlled_contract.ControlledApplicationDryRunTests.tearDown
    path = controlled_contract.ControlledApplicationDryRunTests.path
    review_and_queue = controlled_contract.ControlledApplicationDryRunTests.review_and_queue

    def test_third_attempt_receipt_survives_process_exit_and_public_readback(self):
        _, body, queued = self.review_and_queue("crashed-final-attempt")
        operation_id = queued["operation_id"]
        for _ in range(2):
            self.assertIsNotNone(claim(self.store, operation_id))
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == operation_id).values(
                        lease_expires_at="2000-01-01T00:00:00+00:00"))
            recover_expired(self.store)
        # Never pass provider secrets, user profiles or private DB configuration
        # to the process. Its only workspace is the test's disposable store.
        environment = {key: os.environ[key] for key in (
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG",
        ) if key in os.environ}
        environment.update({
            "MJOBS_OPERATION_MODE": "FIXTURE", "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_OPERATION_TIMEOUT": "180", "MJOBS_OUTPUT_DIR": str(self.root),
        })
        crash_script = """import os, sys
from unittest.mock import patch
from workers import tasks
original = tasks.execute_controlled_dry_run
def save_then_crash(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(23)
with patch('services.applications.controlled_dry_run.run_browser',
           return_value={'documents': [], 'pages': []}):
    tasks.execute_controlled_dry_run = save_then_crash
    tasks.execute_operation(sys.argv[1])
"""
        crashed = subprocess.run(
            [sys.executable, "-c", crash_script, operation_id], cwd=Path(__file__).parents[2],
            env=environment, capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(crashed.returncode, 23, crashed.stderr)
        exact_path = self.path() + "/runs/" + body["request_id"]
        before = self.client.get(exact_path).json()
        self.assertEqual(before["state"], "RUNNING")
        self.assertIsNotNone(before["receipt"])
        self.assertFalse(before["receipt"]["submission_enabled"])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(
                    lease_expires_at="2000-01-01T00:00:00+00:00"))
        resume_script = """import os
from packages.persistence.store import Store
from workers.operations import recover_expired
store = Store(os.environ['MJOBS_DATABASE_URL'])
try:
    assert recover_expired(store) == 1
    assert recover_expired(store) == 0
finally:
    store.engine.dispose()
"""
        resumed = subprocess.run(
            [sys.executable, "-c", resume_script], cwd=Path(__file__).parents[2],
            env=environment, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        final = self.client.get(exact_path).json()
        self.assertEqual(final["state"], "COMPLETED")
        self.assertEqual(final["receipt"], before["receipt"])
        activity = self.client.get(f"/operations/{operation_id}").json()
        self.assertEqual(activity["state"], "COMPLETED")
        self.assertEqual(activity["attempt_count"], 3)
        self.assertEqual(activity["progress"], 100)
        with patch("workers.tasks.execute_controlled_dry_run",
                   side_effect=AssertionError("Committed verification must not replay")):
            with patch.dict(os.environ, environment):
                self.assertEqual(execute_operation(operation_id)["status"],
                                 "ALREADY_CLAIMED_OR_CAPACITY")
        self.assertEqual(self.store.count("application_attempts"), 0)


if __name__ == "__main__":
    unittest.main()
