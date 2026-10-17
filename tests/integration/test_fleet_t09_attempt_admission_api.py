"""Public admission and attempt-history readback remain replay-safe across restart."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import (
    AttemptConflict, attempt_id_for_request, start_attempt,
)
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.operations import claim


class FleetT09AttemptAdmissionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-fleet-t09-attempt-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.database_url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.fixture = fixture_server()
        self.origin = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        discover(self.store, self.origin, "fixture")
        self.application_id = prepare(
            self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        self.endpoint = f"/profiles/fixture/applications/{self.application_id}"

    def test_concurrent_public_replay_and_interrupted_attempt_readback_after_reopen(self):
        app = create_app(self.store, self.origin, self.root, operation_mode="FIXTURE")
        url = self.endpoint + "/mock-run"
        barrier = threading.Barrier(2)

        def admit():
            barrier.wait(timeout=10)
            return client.post(url, json={"submit": False, "request_id": "fleet-t09-replay"})

        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: admit(), range(2)))
            self.assertEqual([response.status_code for response in responses], [202, 202])
            receipts = [response.json() for response in responses]
            operation_id = receipts[0]["operation_id"]
            self.assertEqual(receipts[1]["operation_id"], operation_id)
            self.assertEqual(sum(not receipt["replayed"] for receipt in receipts), 1)
            self.assertEqual(self.store.count("worker_operations"), 1)

            operation_readback = client.get(url + "s/fleet-t09-replay")
            self.assertEqual(operation_readback.status_code, 200, operation_readback.text)
            self.assertEqual(operation_readback.json()["operation_id"], operation_id)
            self.assertEqual(operation_readback.json()["state"], "QUEUED")
            claimed = claim(self.store, operation_id)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["state"], "RUNNING")
            self.assertEqual(claimed["attempt_count"], 1)

            application = self.store.application(self.application_id)
            with self.store.engine.connect() as connection:
                route = dict(connection.execute(select(m.routes).where(
                    m.routes.c.id == application["route_id"])).mappings().one())
                documents = [dict(row) for row in connection.execute(select(m.documents).where(
                    m.documents.c.application_id == self.application_id,
                    m.documents.c.version == application["document_version"])).mappings()]
            reserved = start_attempt(
                self.store, application, route, documents, request_id=operation_id,
                expected_fingerprint=operation_readback.json()["input_fingerprint"])
            expected_attempt_id = attempt_id_for_request(self.application_id, operation_id)
            self.assertEqual(reserved, {"id": expected_attempt_id, "replayed": False})

            conflict = client.post(url, json={"submit": False, "request_id": "competing-intent"})
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(conflict.json()["detail"]["code"], "APPLICATION_ATTEMPT_IN_PROGRESS")

        self.store.engine.dispose()
        reopened = Store(self.database_url)
        try:
            reopened_app = create_app(reopened, self.origin, self.root, operation_mode="FIXTURE")
            history_url = self.endpoint + "/attempt-history"
            with TestClient(reopened_app) as client:
                operation = client.get(url + "s/fleet-t09-replay")
                self.assertEqual(operation.status_code, 200, operation.text)
                self.assertEqual(operation.json()["operation_id"], operation_id)
                self.assertEqual(operation.json()["state"], "RUNNING")
                self.assertTrue(operation.json()["replayed"])
                self.assertFalse(operation.json()["external_submission_enabled"])
                history = client.get(history_url)
                self.assertEqual(history.status_code, 200, history.text)
                self.assertEqual(len(history.json()["attempts"]), 1)
                attempt = history.json()["attempts"][0]
                self.assertEqual(attempt["attempt_id"], expected_attempt_id)
                self.assertEqual(attempt["state"], "RUNNING")
                self.assertEqual(attempt["input_fingerprint"], operation_readback.json()["input_fingerprint"])
                self.assertFalse(attempt["legacy_metadata"])
                self.assertEqual(client.get(history_url.replace("/fixture/", "/other/")).status_code, 404)
                self.assertEqual(client.get((url + "s/fleet-t09-replay").replace("/fixture/", "/other/")).status_code, 404)
                self.assertEqual(client.post(history_url).status_code, 405)
                for secret in (PROFILE["email"], PROFILE["name"], str(self.root)):
                    self.assertNotIn(secret, history.text)
            with self.assertRaisesRegex(AttemptConflict, "APPLICATION_ATTEMPT_IN_PROGRESS"):
                start_attempt(reopened, application, route, documents, request_id=operation_id)
            self.assertIsNone(claim(reopened, operation_id))
            self.assertEqual(reopened.count("application_attempts"), 1)
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
