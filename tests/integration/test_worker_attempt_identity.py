"""Queue/worker fencing and restart policy on durable synthetic SQLite data."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import AttemptConflict, start_attempt, finish_attempt, read_attempt_history
from services.applications.run_requests import queue_mock_run
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.operations import claim, recover_expired, request_cancel, retry_dead_letter, OperationActionError
from workers.tasks import execute_operation


class WorkerAttemptIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-worker-identity-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate(); self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            self.origin = origin
            discover(self.store, origin, "fixture")
        self.app_id = prepare(self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        self.env = {"MJOBS_DATABASE_URL":self.store.url, "MJOBS_OUTPUT_DIR":str(self.root),
                    "MJOBS_MOCK_ORIGIN":self.origin, "MJOBS_OPERATION_MODE":"FIXTURE"}

    def tearDown(self):
        self.store.engine.dispose(); self.temp.cleanup()

    def queue(self, request="worker-intent", submit=False):
        return queue_mock_run(self.store, "fixture", self.app_id, submit=submit, request_id=request)["operation_id"]

    def operation(self, identifier):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one())

    def set_operation(self, identifier, **values):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(**values))

    def reserve_browser(self, operation_id, **extra):
        app = self.store.application(self.app_id)
        with self.store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(m.routes.c.id == app["route_id"])).mappings().one())
            docs = list(connection.execute(select(m.documents).where(m.documents.c.application_id == self.app_id,
                                    m.documents.c.version == app["document_version"])).mappings())
        return start_attempt(self.store, app, route, docs, request_id=operation_id,
                             expected_fingerprint=self.operation(operation_id)["params"]["input_fingerprint"], **extra)

    def expire(self, operation_id):
        self.set_operation(operation_id, lease_expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())

    def test_dead_letter_before_browser_admission_retries_same_operation_identity(self):
        op = self.queue()
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock", side_effect=RuntimeError("private failure")) as first:
            result = execute_operation(op)
        self.assertEqual(result["status"], "DEAD_LETTERED")
        self.assertEqual(first.call_args.kwargs["request_id"], op)
        self.assertEqual(retry_dead_letter(self.store, op)["operation_id"], op)
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock", return_value={"state":"DRY_RUN_COMPLETE"}) as second:
            self.assertEqual(execute_operation(op)["status"], "COMPLETED")
        self.assertEqual(second.call_args.kwargs["request_id"], op)
        self.assertEqual(first.call_args.kwargs["expected_fingerprint"], second.call_args.kwargs["expected_fingerprint"])
        self.assertEqual(self.operation(op)["attempt_count"], 2)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_changed_inputs_block_worker_before_adapter_or_network(self):
        op = self.queue()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(m.applications.c.id == self.app_id).values(document_version="changed-after-review"))
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock") as adapter:
            result = execute_operation(op)
        adapter.assert_not_called()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "APPLICATION_RUN_INPUT_CHANGED")
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_expired_operation_without_browser_admission_can_retry_with_same_key(self):
        op = self.queue(); claim(self.store, op); self.expire(op)
        self.assertEqual(recover_expired(self.store), 1)
        self.assertEqual(self.operation(op)["state"], "QUEUED")
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock", return_value={"state":"DRY_RUN_COMPLETE"}) as adapter:
            self.assertEqual(execute_operation(op)["status"], "COMPLETED")
        self.assertEqual(adapter.call_args.kwargs["request_id"], op)
        self.assertEqual(self.operation(op)["attempt_count"], 2)

    def test_expired_lease_cannot_steal_unfinished_browser_reservation(self):
        op = self.queue(); claim(self.store, op)
        attempt = self.reserve_browser(op)
        original = read_attempt_history(self.store, "fixture", self.app_id)
        self.expire(op); self.store.engine.dispose()
        self.assertEqual(recover_expired(self.store), 1)
        self.assertEqual(self.operation(op)["state"], "BLOCKED")
        self.assertEqual(self.operation(op)["error"], "APPLICATION_ATTEMPT_IN_PROGRESS")
        self.assertEqual(read_attempt_history(self.store, "fixture", self.app_id), original)
        self.assertEqual(self.store.application(self.app_id)["state"], "PREPARED")
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock") as adapter:
            self.assertEqual(execute_operation(op)["status"], "ALREADY_CLAIMED_OR_CAPACITY")
        adapter.assert_not_called()
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_ATTEMPT_IN_PROGRESS"):
            self.queue("competing-new-intent")
        self.assertEqual(original["attempts"][0]["attempt_id"], attempt["id"])

    def test_failed_browser_request_requires_new_reviewed_intent_not_dead_letter_replay(self):
        op = self.queue(); claim(self.store, op)
        reservation = self.reserve_browser(op)
        finish_attempt(self.store, reservation["id"], self.store.application(self.app_id), error=RuntimeError("private"))
        before = read_attempt_history(self.store, "fixture", self.app_id)
        self.set_operation(op, state="DEAD_LETTERED", error="BROWSER_TIMEOUT", lease_expires_at=None)
        with self.assertRaisesRegex(OperationActionError, "APPLICATION_ATTEMPT_REVIEW_REQUIRED"):
            retry_dead_letter(self.store, op)
        with TestClient(create_app(self.store, self.origin, self.root, operation_mode="FIXTURE")) as client:
            result = client.get("/operations/dead-letter-queue?profile_id=fixture").json()["operations"][0]
            self.assertFalse(result["retryable"])
            self.assertEqual(result["retry_blocked_reason"], "APPLICATION_ATTEMPT_REVIEW_REQUIRED")
        new_op = self.queue("reviewed-new-execution")
        self.assertNotEqual(new_op, op)
        self.assertEqual(read_attempt_history(self.store, "fixture", self.app_id), before)

    def test_cancelled_queue_replay_remains_cancelled_and_never_calls_adapter(self):
        op = self.queue()
        request_cancel(self.store, op)
        self.assertEqual(self.queue(), op)
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock") as adapter:
            self.assertEqual(execute_operation(op)["status"], "ALREADY_CLAIMED_OR_CAPACITY")
        adapter.assert_not_called()
        self.assertEqual(self.operation(op)["attempt_count"], 0)
        self.assertEqual(self.operation(op)["state"], "CANCELLED")

    def test_cancellation_after_submit_reservation_requires_receipt_reconciliation(self):
        op = self.queue(submit=True); claim(self.store, op)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(m.applications.c.id == self.app_id).values(state="SUBMITTING"))
        self.assertEqual(request_cancel(self.store, op)["reason"], "RECONCILIATION_REQUIRED")
        self.assertFalse(self.operation(op)["cancel_requested"])
        self.assertEqual(self.store.application(self.app_id)["state"], "SUBMITTING")

    def test_frozen_snapshot_is_checked_again_at_physical_admission(self):
        op = self.queue()
        params = self.operation(op)["params"]
        app = self.store.application(self.app_id)
        with self.store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(m.routes.c.id == app["route_id"])).mappings().one())
            docs = list(connection.execute(select(m.documents).where(m.documents.c.application_id == self.app_id,
                                    m.documents.c.version == app["document_version"])).mappings())
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == app["route_id"]).values(requisition="new-unreviewed-job"))
        # Even an adapter holding stale metadata cannot admit the old review.
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_ATTEMPT_INPUT_CHANGED"):
            start_attempt(self.store, app, route, docs, request_id=op, expected_fingerprint=params["input_fingerprint"])
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_tampered_queue_contract_fails_closed(self):
        op = self.queue()
        params = self.operation(op)["params"]
        self.set_operation(op, params={**params, "submit":True})
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock") as adapter:
            result = execute_operation(op)
        adapter.assert_not_called()
        self.assertEqual(result["error_category"], "APPLICATION_RUN_INPUT_CHANGED")

    def test_unknown_contract_version_cannot_fall_back_to_legacy_execution(self):
        op = self.queue(); params = self.operation(op)["params"]
        self.set_operation(op, params={**params, "run_contract_version":2})
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock") as adapter:
            result = execute_operation(op)
        adapter.assert_not_called()
        self.assertEqual(result["error_category"], "APPLICATION_RUN_CONTRACT_UNSUPPORTED")


    def test_dead_letter_retry_cannot_compete_with_a_new_reviewed_intent(self):
        old = self.queue()
        self.set_operation(old, state="DEAD_LETTERED", error="OPERATION_TIMEOUT")
        new = self.queue("new-reviewed-intent")
        with self.assertRaisesRegex(OperationActionError, "APPLICATION_RUN_IN_PROGRESS"):
            retry_dead_letter(self.store, old)
        self.assertEqual(self.operation(old)["state"], "DEAD_LETTERED")
        self.assertEqual(self.operation(new)["state"], "QUEUED")


if __name__ == "__main__":
    unittest.main()
