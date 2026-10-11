"""Dependency health requires fresh scheduler ticks and every exact queue."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import json
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from workers.operations import QUEUES


class RuntimeReadinessApiTests(unittest.TestCase):
    def test_scheduler_and_company_worker_gate_actual_readiness(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-runtime-readiness-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'readiness.db').as_posix()}")
            store.migrate()
            worker = Mock()
            worker.worker_ttl = 420
            worker.last_heartbeat = datetime.now(timezone.utc)
            worker.get_state.return_value = "idle"
            worker.queue_names.return_value = sorted(set(QUEUES.values()))
            redis = Mock()
            redis.ping.return_value = True
            redis.get.return_value = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "interval_seconds": 30}).encode()
            try:
                with patch("apps.api.main.Redis.from_url", return_value=redis), \
                        patch("rq.Worker.all", return_value=[worker]), \
                        TestClient(create_app(store, "http://127.0.0.1:1", root, operation_mode="PERSONAL")) as client:
                    ready = client.get("/readiness")
                    self.assertEqual(ready.status_code, 200, ready.text)
                    alias = client.get("/ready")
                    self.assertEqual(alias.status_code, 200, alias.text)
                    self.assertEqual(alias.json()["checks"], ready.json()["checks"])
                    self.assertTrue(ready.json()["checks"]["scheduler"])
                    self.assertTrue(ready.json()["checks"]["workers"]["mjobs-company-discovery"])
                    redis.get.return_value = None
                    stopped = client.get("/readiness")
                    self.assertEqual(stopped.status_code, 503)
                    self.assertFalse(stopped.json()["checks"]["scheduler"])
                    self.assertEqual(client.get("/ready").status_code, 503)
                    redis.get.return_value = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "interval_seconds": 30}).encode()
                    worker.queue_names.return_value = [name for name in QUEUES.values() if name != "mjobs-company-discovery"]
                    missing = client.get("/readiness")
                    self.assertEqual(missing.status_code, 503)
                    self.assertFalse(missing.json()["checks"]["workers"]["mjobs-company-discovery"])
                    self.assertFalse(missing.json()["checks"]["worker"])
            finally:
                store.engine.dispose()
