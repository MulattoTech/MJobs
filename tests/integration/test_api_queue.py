"""API and outbox tests exercise persistence with the queue unavailable."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from apps.api.main import create_app, create_from_env
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation, dispatch_pending
from fixtures.mock_server import fixture_server
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from workers.tasks import execute_operation


class ApiQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_queue_disconnect_preserves_one_durable_operation(self):
        first = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        second = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        self.assertEqual(first, second)
        result = dispatch_pending(self.store, "redis://127.0.0.1:1/0")
        self.assertEqual(result["status"], "QUEUE_UNAVAILABLE")
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_concurrent_requests_share_one_durable_operation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            identifiers = list(pool.map(lambda _: enqueue_operation(self.store, "DISCOVER", "fixture", {}), range(2)))
        self.assertEqual(identifiers[0], identifiers[1])
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_api_saves_explicit_facts_and_reports_durable_queueing(self):
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
            response = client.put("/profiles", json=PROFILE)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["version"])
            queued = client.post("/discovery", json={"profile_id": "fixture"})
            self.assertEqual(queued.status_code, 202)
            operation = client.get("/operations/" + queued.json()["operation_id"])
            self.assertEqual(operation.json()["state"], "QUEUED")
            self.assertEqual(queued.json()["state"], "QUEUED")
            self.assertEqual(client.get("/readiness").status_code, 503)
            self.assertEqual(client.get("/settings").json()["external_submission_enabled"], False)

    def test_untrusted_browser_origin_and_string_verification_are_rejected(self):
        with self.assertRaises(ValueError):
            create_app(self.store, "https://external.example", self.root, operation_mode="FIXTURE")
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root, operation_mode="FIXTURE")) as client:
            invalid = dict(PROFILE, facts=[dict(PROFILE["facts"][0], verified="yes")])
            self.assertEqual(client.put("/profiles", json=invalid).status_code, 422)

    def test_personal_mode_rejects_fixture_actions_and_keeps_public_collection(self):
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root)) as client:
            self.assertEqual(client.get("/settings").json()["operation_mode"], "PERSONAL")
            self.assertNotIn("mock", client.get("/readiness").json()["checks"])
            self.assertEqual(client.post("/discovery", json={"profile_id": "fixture"}).status_code, 409)
            self.assertEqual(client.post("/applications/nonexistent/mock-run",
                                         json={"submit": True}).status_code, 409)
            source = client.put("/sources", json={"tenant": "example"})
            self.assertEqual(source.status_code, 200)
            collect = client.post("/sources/" + source.json()["id"] + "/collect",
                                  json={"profile_id": "fixture"})
            self.assertEqual(collect.status_code, 202)
        self.assertEqual(self.store.count("worker_operations"), 1)
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url, MJOBS_MOCK_ORIGIN="http://127.0.0.1:8090",
                        MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="PERSONAL"), \
                patch("workers.tasks.collect_source") as collect_source:
            self.assertEqual(execute_operation(collect.json()["operation_id"])["status"], "COMPLETED")
            collect_source.assert_called_once()

    def test_personal_runtime_rejects_fixture_database(self):
        self.store.save_profile(PROFILE)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                        MJOBS_MOCK_ORIGIN="http://mock:8090", MJOBS_OUTPUT_DIR=str(self.root),
                        MJOBS_REDIS_URL="redis://127.0.0.1:1/0", MJOBS_OPERATION_MODE="PERSONAL"):
            with self.assertRaisesRegex(RuntimeError, "separate Compose project"):
                create_from_env()

    def test_readiness_requires_the_application_queue_worker(self):
        worker = Mock()
        worker.get_state.return_value = "idle"
        worker.queue_names.return_value = ["unrelated"]
        with fixture_server() as origin, patch("apps.api.main.Redis") as redis, patch("apps.api.main.Worker.all", return_value=[worker]):
            redis.from_url.return_value.ping.return_value = True
            with TestClient(create_app(self.store, origin, self.root, operation_mode="FIXTURE")) as client:
                response = client.get("/readiness")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["worker"])

    def test_readiness_degrades_on_invalid_worker_metadata(self):
        with fixture_server() as origin, patch("apps.api.main.Redis") as redis, patch("apps.api.main.Worker.all", side_effect=ValueError("Invalid worker metadata")):
            redis.from_url.return_value.ping.return_value = True
            with TestClient(create_app(self.store, origin, self.root, operation_mode="FIXTURE")) as client:
                response = client.get("/readiness")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["worker"])

    def test_preparation_is_durable_background_work_and_cancel_is_visible(self):
        with fixture_server() as origin, TestClient(create_app(self.store, origin, self.root, operation_mode="FIXTURE")) as client:
            client.put("/profiles", json=PROFILE).raise_for_status()
            discover(self.store, origin, "fixture")
            job = self.store.jobs()[0]
            response = client.post("/applications", json={"profile_id": "fixture", "job_id": job["id"]})
            self.assertEqual(response.status_code, 202)
            queued = response.json()
            self.assertEqual(queued["state"], "QUEUED")
            self.assertEqual(self.store.count("applications"), 0)
            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url, MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="FIXTURE"):
                self.assertEqual(execute_operation(queued["operation_id"])["status"], "COMPLETED")
            self.assertEqual(client.get("/profiles/fixture/applications/" + queued["application_id"]).json()["state"], "PREPARED")
            discovery = client.post("/discovery", json={"profile_id": "fixture"}).json()
            cancelled = client.post("/operations/" + discovery["operation_id"] + "/cancel")
            self.assertEqual(cancelled.status_code, 202)
            self.assertEqual(cancelled.json()["state"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()
