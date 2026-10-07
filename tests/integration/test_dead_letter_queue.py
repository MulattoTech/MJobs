"""Dead-letter actions preserve the durable operation identity and audit trail."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation


class DeadLetterQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root, operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def dead_letter(self):
        identifier = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                state="DEAD_LETTERED", error="RETRY_LIMIT", attempt_count=3,
                last_attempt_at="2026-10-07T00:00:00Z", dead_lettered_at="2026-10-07T00:01:00Z"))
        return identifier

    def operation(self, identifier):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == identifier)).mappings().one())

    def audit_actions(self, identifier):
        with self.store.engine.connect() as connection:
            return [row.action for row in connection.execute(select(m.audit.c.action).where(
                m.audit.c.entity_id == identifier).order_by(m.audit.c.timestamp))]

    def test_explicit_retry_reuses_operation_id_and_records_durable_audit(self):
        identifier = self.dead_letter()

        response = self.client.post(f"/operations/{identifier}/retry")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["operation_id"], identifier)
        self.assertEqual(self.operation(identifier)["state"], "QUEUED")
        self.assertEqual(self.operation(identifier)["attempt_count"], 3)
        self.assertEqual(self.operation(identifier)["dead_lettered_at"], "2026-10-07T00:01:00Z")
        self.assertEqual(self.operation(identifier)["last_attempt_at"], "2026-10-07T00:00:00Z")
        self.assertIn("DEAD_LETTER_RETRY_REQUESTED", self.audit_actions(identifier))

    def test_discard_is_terminal_and_preserves_failure_reason(self):
        identifier = self.dead_letter()

        response = self.client.post(f"/operations/{identifier}/discard")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "DISCARDED")
        self.assertEqual(self.operation(identifier)["state"], "DISCARDED")
        self.assertEqual(self.operation(identifier)["error"], "RETRY_LIMIT")
        self.assertIsNotNone(self.operation(identifier)["discarded_at"])
        self.assertIn("DEAD_LETTER_DISCARDED", self.audit_actions(identifier))
        self.assertEqual(self.client.post(f"/operations/{identifier}/retry").status_code, 409)

    def test_queue_reports_accurate_count_and_is_profile_scoped(self):
        first = self.dead_letter()
        second = enqueue_operation(self.store, "DISCOVER", "another-profile", {})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == second).values(
                state="DEAD_LETTERED", error="SOURCE_UNAVAILABLE", attempt_count=2))

        response = self.client.get("/operations/dead-letter-queue", params={"profile_id": "fixture"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual([row["id"] for row in response.json()["operations"]], [first])
        self.assertEqual(self.client.get("/operations/dead-letter-queue").json()["count"], 2)

    def test_ambiguous_application_submission_cannot_be_retried(self):
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs("fixture")[0]
        application_id = prepare(self.store, job["id"], "fixture", self.root)
        operation_id = enqueue_operation(self.store, "APPLY_MOCK", application_id, {"submit": True})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(m.applications.c.id == application_id).values(
                state="SUBMISSION_UNCONFIRMED"))
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                state="DEAD_LETTERED", error="RETRY_LIMIT", attempt_count=3))

        response = self.client.post(f"/operations/{operation_id}/retry")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "RECONCILIATION_REQUIRED")
        self.assertEqual(self.operation(operation_id)["state"], "DEAD_LETTERED")
        self.assertEqual(self.operation(operation_id)["attempt_count"], 3)
        listed = self.client.get("/operations/dead-letter-queue", params={"profile_id": "fixture"})
        operation = next(item for item in listed.json()["operations"] if item["id"] == operation_id)
        self.assertFalse(operation["retryable"])
        self.assertEqual(operation["retry_blocked_reason"], "RECONCILIATION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
