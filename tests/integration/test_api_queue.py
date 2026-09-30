"""API and outbox tests exercise persistence with the queue unavailable."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation, dispatch_pending
from fixtures.mock_server import fixture_server


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
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root, "redis://127.0.0.1:1/0")) as client:
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
            create_app(self.store, "https://external.example", self.root)
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root)) as client:
            invalid = dict(PROFILE, facts=[dict(PROFILE["facts"][0], verified="yes")])
            self.assertEqual(client.put("/profiles", json=invalid).status_code, 422)

    def test_readiness_requires_the_application_queue_worker(self):
        worker = Mock()
        worker.get_state.return_value = "idle"
        worker.queue_names.return_value = ["unrelated"]
        with fixture_server() as origin, patch("apps.api.main.Redis") as redis, patch("apps.api.main.Worker.all", return_value=[worker]):
            redis.from_url.return_value.ping.return_value = True
            with TestClient(create_app(self.store, origin, self.root)) as client:
                response = client.get("/readiness")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["worker"])

    def test_readiness_degrades_on_invalid_worker_metadata(self):
        with fixture_server() as origin, patch("apps.api.main.Redis") as redis, patch("apps.api.main.Worker.all", side_effect=ValueError("Invalid worker metadata")):
            redis.from_url.return_value.ping.return_value = True
            with TestClient(create_app(self.store, origin, self.root)) as client:
                response = client.get("/readiness")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["worker"])


if __name__ == "__main__":
    unittest.main()
