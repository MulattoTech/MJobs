"""Safe timeout classes survive operation and public attempt-history readback."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.pool import QueuePool

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import finish_attempt, start_attempt
from services.applications.run_requests import queue_mock_run
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class TimeoutFailureReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-timeout-readback-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'timeout.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        with fixture_server() as origin:
            self.origin = origin
            discover(self.store, origin, "fixture")
        self.app_id = prepare(
            self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root
        )
        self.environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_MOCK_ORIGIN": self.origin,
            "MJOBS_OPERATION_MODE": "FIXTURE",
        }
        self.client = TestClient(
            create_app(self.store, self.origin, self.root, operation_mode="FIXTURE")
        )

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _real_pool_timeout(self):
        engine = create_engine(
            "sqlite://",
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.01,
        )
        held = engine.connect()
        try:
            with self.assertRaises(SQLAlchemyTimeoutError) as caught:
                engine.connect()
            return caught.exception
        finally:
            held.close()
            engine.dispose()

    def test_worker_operation_receipt_keeps_pool_timeout_distinct_and_sanitized(self):
        operation_id = queue_mock_run(
            self.store, "fixture", self.app_id, submit=False,
            request_id="pool-timeout-public-readback",
        )["operation_id"]
        pool_error = self._real_pool_timeout()
        message_sentinel = "PRIVATE_DB_DETAIL_SENTINEL"
        pool_error.args = (message_sentinel,)

        with patch.dict(os.environ, self.environment), patch(
            "workers.tasks.apply_mock", side_effect=pool_error
        ):
            result = execute_operation(operation_id)

        self.assertEqual(result["status"], "DEAD_LETTERED")
        receipt = self.client.get(f"/operations/{operation_id}")
        self.assertEqual(receipt.status_code, 200)
        public = receipt.json()
        self.assertEqual(public["error"], "DATABASE_POOL_TIMEOUT")
        self.assertNotIn(message_sentinel, json.dumps(public))
        self.assertNotIn("sqlalchemy.exc", json.dumps(public))

    def test_public_attempt_history_distinguishes_real_browser_and_ambiguous_timeouts(self):
        application = self.store.application(self.app_id)
        with self.store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == application["route_id"]
            )).mappings().one())
            documents = [dict(row) for row in connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.app_id,
                m.documents.c.version == application["document_version"],
            )).mappings()]

        cases = [
            ("history-playwright-timeout", PlaywrightTimeoutError("PRIVATE_BROWSER_DETAIL"),
             "PLAYWRIGHT_TIMEOUT"),
            ("history-legacy-timeout", TimeoutError("PRIVATE_LEGACY_DETAIL"),
             "TIMEOUT_UNCLASSIFIED"),
        ]
        expected = {}
        for request_id, error, code in cases:
            reserved = start_attempt(
                self.store, application, route, documents,
                request_id=request_id, submit=False,
            )
            expected[reserved["id"]] = code
            finish_attempt(self.store, reserved["id"], application, error=error)

        response = self.client.get(
            f"/profiles/fixture/applications/{self.app_id}/attempt-history"
        )
        self.assertEqual(response.status_code, 200)
        attempts = response.json()["attempts"]
        self.assertEqual(
            {row["attempt_id"]: row["error_code"] for row in attempts}, expected
        )
        serialized = json.dumps(attempts)
        for private in ("PRIVATE_BROWSER_DETAIL", "PRIVATE_LEGACY_DETAIL"):
            self.assertNotIn(private, serialized)
        self.assertNotIn("playwright._impl", serialized)
        self.assertNotIn("sqlalchemy.exc", serialized)


if __name__ == "__main__":
    unittest.main()
