"""Only a proven, fenced worker replay may reconcile a crashed browser attempt."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import STARTED, attempt_id_for_request, read_attempt_history, _event_id
from services.applications.confirmation_recovery import ConfirmationConflict, _guard
from services.applications.run_requests import queue_mock_run
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.operations import claim, recover_expired
from workers.tasks import execute_operation


class RecoveredApplicationAttemptProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-recovered-attempt-proof-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.profile_id = "proof-" + uuid4().hex[:12]
        self.store.save_profile(dict(PROFILE, id=self.profile_id,
                                     email=f"{self.profile_id}@example.test"))
        self.fixture = fixture_server()
        self.origin = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        discover(self.store, self.origin, self.profile_id)
        self.application_id = prepare(
            self.store, self.store.jobs(self.profile_id)[0]["id"], self.profile_id, self.root)
        self.operation_id = "proof-operation-" + uuid4().hex[:12]
        self.operation_id = queue_mock_run(
            self.store, self.profile_id, self.application_id,
            submit=True, request_id=self.operation_id)["operation_id"]
        self.attempt_id = attempt_id_for_request(self.application_id, self.operation_id)
        self._start_physical_attempt()
        self._simulate_crash_recovery_and_receipt_retry()

    def _start_physical_attempt(self):
        application = self.store.application(self.application_id)
        with self.store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == application["route_id"])).mappings().one())
            documents = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.version == application["document_version"])).mappings())
        from services.applications.attempt_history import start_attempt
        start_attempt(self.store, application, route, documents, submit=True,
                      request_id=self.operation_id,
                      expected_fingerprint=self._operation()["params"]["input_fingerprint"])
        self.store.transition(self.application_id, ["PREPARED"], "SUBMITTING")

    def _simulate_crash_recovery_and_receipt_retry(self):
        self.assertEqual(claim(self.store, self.operation_id)["attempt_count"], 1)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(
                    lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
        self.assertEqual(recover_expired(self.store), 1)
        recovered = self._operation()
        self.assertEqual((recovered["state"], recovered["error"], recovered["attempt_count"]),
                         ("QUEUED", "RECONCILIATION_REQUIRED", 1))
        with patch.dict("os.environ", {
                "MJOBS_DATABASE_URL": self.store.url,
                "MJOBS_MOCK_ORIGIN": self.origin,
                "MJOBS_OUTPUT_DIR": str(self.root),
                "MJOBS_OPERATION_MODE": "FIXTURE",
        }), patch("services.applications.mock_adapter._receipt", return_value=None), patch(
                "services.applications.mock_adapter.sync_playwright",
                side_effect=AssertionError("Receipt retry must not open a browser")):
            self.assertEqual(execute_operation(self.operation_id)["status"], "BLOCKED")

    def _operation(self):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == self.operation_id)).mappings().one())

    def _guard(self):
        application = self.store.application(self.application_id)
        with self.store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == application["route_id"])).mappings().one())
            _guard(connection, application, route)

    def _start_audit(self):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.audit).where(
                m.audit.c.id == _event_id(STARTED, self.attempt_id))).mappings().one())

    def test_fenced_same_request_replay_allows_read_only_reconcile_without_rewriting_attempt(self):
        history_before = read_attempt_history(self.store, self.profile_id, self.application_id)
        self.assertEqual(self._operation()["state"], "BLOCKED")
        self.assertEqual(history_before["attempts"][0]["attempt_id"], self.attempt_id)
        self.assertEqual(history_before["attempts"][0]["state"], "RUNNING")
        self._guard()
        self.assertEqual(read_attempt_history(self.store, self.profile_id, self.application_id), history_before)
        self.assertEqual(self._start_audit()["payload"]["attempt_id"], self.attempt_id)

    def test_fenced_successful_receipt_recovery_allows_read_only_reconcile(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(
                    state="COMPLETED", error=None, current_step="COMPLETED", progress=100,
                    recovery_metadata={"action": "NONE"}))
            finish = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.operation_id,
                m.audit.c.action == "OPERATION_ATTEMPT_FINISHED")).mappings().one()
            connection.execute(update(m.audit).where(m.audit.c.id == finish["id"]).values(
                payload={"state": "COMPLETED", "error": None, "attempt_count": 2}))
        self._guard()
        history = read_attempt_history(self.store, self.profile_id, self.application_id)
        self.assertEqual(history["attempts"][0]["state"], "RUNNING")

    def test_live_operation_is_never_authorized_by_stale_attempt_proof(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(
                    state="RUNNING", completed_at=None,
                    lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()))
        with self.assertRaisesRegex(ConfirmationConflict, "EXECUTION_STILL_RESERVED"):
            self._guard()

    def test_queued_operation_is_never_authorized_by_stale_attempt_proof(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(
                    state="QUEUED", completed_at=None, lease_expires_at=None,
                    recovery_metadata={"action": "RESUME"}))
        with self.assertRaisesRegex(ConfirmationConflict, "EXECUTION_STILL_RESERVED"):
            self._guard()

    def test_missing_lease_recovery_audit_keeps_attempt_reserved(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.delete().where(
                m.audit.c.entity_id == self.operation_id,
                m.audit.c.action == "LEASE_RECOVERY"))
        with self.assertRaisesRegex(ConfirmationConflict, "EXECUTION_STILL_RESERVED"):
            self._guard()

    def test_mismatched_started_snapshot_keeps_attempt_reserved(self):
        start_event = self._start_audit()
        payload = dict(start_event["payload"], input_fingerprint="0" * 64)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.audit).where(
                m.audit.c.id == start_event["id"]).values(payload=payload))
        with self.assertRaisesRegex(ConfirmationConflict, "EXECUTION_STILL_RESERVED"):
            self._guard()

    def test_started_audit_scoped_to_another_application_keeps_attempt_reserved(self):
        start_event = self._start_audit()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.audit).where(
                m.audit.c.id == start_event["id"]).values(entity_id="foreign-application-proof"))
        with self.assertRaisesRegex(ConfirmationConflict, "EXECUTION_STILL_RESERVED"):
            self._guard()

    def test_terminal_operation_without_recovered_second_claim_keeps_attempt_reserved(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(attempt_count=1))
        with self.assertRaisesRegex(ConfirmationConflict, "EXECUTION_STILL_RESERVED"):
            self._guard()

    def test_additional_unlinked_running_attempt_keeps_application_reserved(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.attempts.insert().values(
                id="unlinked-running-attempt", application_id=self.application_id,
                state="RUNNING", steps=[], started_at=datetime.now(timezone.utc).isoformat()))
        with self.assertRaisesRegex(ConfirmationConflict, "EXECUTION_STILL_RESERVED"):
            self._guard()


if __name__ == "__main__":
    unittest.main()
