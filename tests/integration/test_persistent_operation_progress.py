"""Profile-scoped readback for durable operation progress."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.operations.progress import record_operation_progress
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation


class PersistentOperationProgressApiTests(unittest.TestCase):
    def test_profile_progress_exposes_retry_deadline_without_internal_payloads(self):
        identifier = self._operation("fixture", "PRIVATE_OPERATION_PARAMS_SENTINEL")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                state="QUEUED", current_step="RETRY_WAIT", next_attempt_at="2026-10-11T12:00:00+00:00"))
        row = self._read("fixture")[0]
        self.assertEqual(row["next_attempt_at"], "2026-10-11T12:00:00+00:00")
        self.assertNotIn("PRIVATE_OPERATION_PARAMS_SENTINEL", str(row))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-operation-progress-api-")
        self.root = Path(self.directory.name)
        self.database_url = f"sqlite:///{(self.root / 'operations.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        other = deepcopy(PROFILE)
        other.update(id="other-profile", name="Other Fixture", email="other@example.test")
        self.store.save_profile(other)
        self.client = self._client()

    def _client(self):
        return TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _operation(self, profile_id, marker):
        return enqueue_operation(self.store, "DISCOVER", profile_id, {"request_marker": marker})

    def _read(self, profile_id):
        response = self.client.get(f"/profiles/{profile_id}/operations/progress")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"operations"})
        return response.json()["operations"]

    def test_profile_progress_is_durable_and_exposes_only_the_safe_read_model(self):
        operation_id = self._operation("fixture", "PRIVATE_OPERATION_PARAMS_SENTINEL")
        public_progress = record_operation_progress(
            self.store,
            operation_id,
            current_step="FETCH_SOURCE",
            progress=38,
            state="RUNNING",
            error="PRIVATE_OPERATION_ERROR_SENTINEL",
            retry_count=2,
            recovery_metadata={
                "action": "RETRY",
                "reason_code": "QUEUE_TIMEOUT",
                "retry_after_seconds": 600,
                "retryable": True,
            },
        )
        self.assertEqual(public_progress["progress"], 38)

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        operations = self._read("fixture")
        self.assertEqual(len(operations), 1)
        operation = operations[0]
        self.assertEqual(set(operation), {
            "operation_id", "operation_type", "entity_id", "state", "current_step",
            "progress", "started_at", "updated_at", "completed_at", "retry_count",
            "recovery_summary", "next_attempt_at",
        })
        self.assertEqual(operation["operation_id"], operation_id)
        self.assertEqual(operation["operation_type"], "DISCOVER")
        self.assertEqual(operation["entity_id"], "fixture")
        self.assertEqual(operation["state"], "RUNNING")
        self.assertEqual(operation["current_step"], "FETCH_SOURCE")
        self.assertEqual(operation["progress"], 38)
        self.assertIsNotNone(operation["updated_at"])
        self.assertIsNone(operation["completed_at"])
        self.assertEqual(operation["retry_count"], 2)
        self.assertEqual(operation["recovery_summary"], "RETRY: QUEUE_TIMEOUT")
        self.assertIsNotNone(operation["started_at"])
        self.assertNotIn("params", operation)
        self.assertNotIn("error", operation)
        self.assertNotIn("recovery_metadata", operation)
        serialized = self.client.get("/profiles/fixture/operations/progress").text
        self.assertNotIn("PRIVATE_OPERATION_PARAMS_SENTINEL", serialized)
        self.assertNotIn("PRIVATE_OPERATION_ERROR_SENTINEL", serialized)

    def test_progress_listing_is_profile_scoped_and_hides_foreign_operations(self):
        fixture_operation = self._operation("fixture", "fixture-only")
        other_operation = self._operation("other-profile", "other-only")

        # Application operations use an application ID as entity_id. Verify the
        # endpoint maps those IDs through the owning profile before returning them.
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            discover(self.store, origin, "other-profile")
        job_id = self.store.jobs("fixture")[0]["id"]
        fixture_application = prepare(self.store, job_id, "fixture", self.root)
        other_application = prepare(self.store, job_id, "other-profile", self.root)
        fixture_application_operation = enqueue_operation(
            self.store, "APPLY_MOCK", fixture_application, {"submit": False})
        other_application_operation = enqueue_operation(
            self.store, "APPLY_MOCK", other_application, {"submit": False})

        fixture_rows = self._read("fixture")
        other_rows = self._read("other-profile")

        fixture_ids = {row["operation_id"] for row in fixture_rows}
        other_ids = {row["operation_id"] for row in other_rows}
        self.assertEqual(fixture_ids, {fixture_operation, fixture_application_operation})
        self.assertEqual(other_ids, {other_operation, other_application_operation})
        self.assertTrue(fixture_ids.isdisjoint(other_ids))

    def test_queued_operations_without_progress_return_safe_defaults(self):
        operation_id = self._operation("fixture", "queued-without-progress")

        rows = self._read("fixture")

        operation = next(row for row in rows if row["operation_id"] == operation_id)
        self.assertEqual(operation["state"], "QUEUED")
        self.assertEqual(operation["progress"], 0)
        self.assertIsNone(operation["current_step"])
        self.assertIsNone(operation["started_at"])
        self.assertIsNone(operation["completed_at"])
        self.assertEqual(operation["retry_count"], 0)
        self.assertEqual(operation["recovery_summary"], "")

    def test_invalid_progress_update_is_rejected_without_changing_readback(self):
        operation_id = self._operation("fixture", "invalid-progress")
        record_operation_progress(
            self.store, operation_id, current_step="FETCH_SOURCE", progress=30, state="RUNNING")
        before = next(row for row in self._read("fixture") if row["operation_id"] == operation_id)

        with self.assertRaises(ValueError):
            record_operation_progress(
                self.store, operation_id, current_step="FETCH_SOURCE", progress=101)

        after = next(row for row in self._read("fixture") if row["operation_id"] == operation_id)
        self.assertEqual(after, before)

    def test_unknown_profile_returns_not_found(self):
        response = self.client.get("/profiles/missing-profile/operations/progress")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
