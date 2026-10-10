"""Actual Chromium plus worker process exit, cancellation and receipt-only recovery."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import read_attempt_history
from services.applications.mock_adapter import apply_mock
from services.applications.run_requests import queue_mock_run
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.operations import recover_expired, request_cancel, retry_dead_letter, OperationActionError
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


class WorkerBrowserRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-worker-browser-")
        self.root = Path(self.temp.name)
        self.fixture_context = fixture_server()
        self.origin = self.fixture_context.__enter__()
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate(); self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        discover(self.store, self.origin, "fixture")
        self.app_id = prepare(self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        self.env = {"MJOBS_DATABASE_URL":self.store.url, "MJOBS_OUTPUT_DIR":str(self.root),
                    "MJOBS_MOCK_ORIGIN":self.origin, "MJOBS_OPERATION_MODE":"FIXTURE", "PYTHONUTF8":"1"}

    def tearDown(self):
        self.store.engine.dispose()
        self.fixture_context.__exit__(None, None, None)
        self.temp.cleanup()

    def queue(self, request="reviewed-browser-run", submit=False):
        return queue_mock_run(self.store, "fixture", self.app_id, submit=submit, request_id=request)["operation_id"]

    def operation(self, identifier):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one())

    def counts(self):
        return httpx.get(self.origin + "/_test/request-counts", trust_env=False).json()

    def reopen(self):
        url = self.store.url
        self.store.engine.dispose()
        self.store = Store(url)

    def test_real_worker_exit_after_browser_completion_replays_without_second_browser(self):
        operation_id = self.queue()
        # Only this test-owned child exits. The browser has already closed and
        # its immutable completion committed; the worker acknowledgement has not.
        code = '''import os, sys
from workers import tasks
original = tasks.apply_mock
def exit_after_browser(*args, **kwargs):
    result = original(*args, **kwargs)
    assert result["state"] == "DRY_RUN_COMPLETE"
    os._exit(73)
tasks.apply_mock = exit_after_browser
tasks.execute_operation(sys.argv[1])
'''
        child = subprocess.run([sys.executable, "-c", code, operation_id], cwd=ROOT,
                               env={**os.environ, **self.env}, capture_output=True, text=True, timeout=90)
        self.assertEqual(child.returncode, 73, child.stderr[-2000:])
        self.assertEqual(self.operation(operation_id)["state"], "RUNNING")
        before = read_attempt_history(self.store, "fixture", self.app_id)
        self.assertEqual(len(before["attempts"]), 1)
        self.assertEqual(before["attempts"][0]["state"], "DRY_RUN_COMPLETE")
        self.assertTrue(before["attempts"][0]["finished_at"])
        self.assertEqual(self.counts(), {"review":1, "submit":0})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                lease_expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()))
        self.reopen()
        self.assertEqual(recover_expired(self.store), 1)
        with patch.dict("os.environ", self.env), patch("services.applications.mock_adapter.sync_playwright",
                side_effect=AssertionError("Completed physical attempt must not relaunch Chromium")):
            self.assertEqual(execute_operation(operation_id)["status"], "COMPLETED")
            self.assertEqual(execute_operation(operation_id)["status"], "ALREADY_CLAIMED_OR_CAPACITY")
        self.assertEqual(self.operation(operation_id)["attempt_count"], 2)
        self.assertEqual(read_attempt_history(self.store, "fixture", self.app_id), before)
        self.assertEqual(self.counts(), {"review":1, "submit":0})
        self.assertEqual(self.queue(), operation_id)
        self.assertEqual(self.operation(operation_id)["state"], "COMPLETED")

    def test_cooperative_browser_cancel_preserves_history_and_requires_new_reviewed_run(self):
        operation_id = self.queue()
        cancelled = []
        def cancelling_adapter(*args, **kwargs):
            original_check = kwargs["check"]
            def check():
                if not cancelled and self.store.application(self.app_id)["state"] == "FILLING":
                    cancelled.append(request_cancel(self.store, operation_id))
                original_check()
            check.remaining_seconds = original_check.remaining_seconds
            return apply_mock(*args, **{**kwargs, "check":check})
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock", side_effect=cancelling_adapter):
            result = execute_operation(operation_id)
        self.assertTrue(cancelled)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(self.counts(), {"review":0, "submit":0})
        before = read_attempt_history(self.store, "fixture", self.app_id)["attempts"][0]
        self.assertEqual(before["error_code"], "OPERATION_CANCELLED")
        self.assertTrue(before["finished_at"])
        self.reopen()
        self.assertEqual(self.queue(), operation_id)
        with patch.dict("os.environ", self.env), patch("workers.tasks.apply_mock", side_effect=AssertionError("Cancelled replay ran")):
            self.assertEqual(execute_operation(operation_id)["status"], "ALREADY_CLAIMED_OR_CAPACITY")
        new_id = self.queue("explicit-new-run-after-review")
        with patch.dict("os.environ", self.env):
            self.assertEqual(execute_operation(new_id)["status"], "COMPLETED")
        history = read_attempt_history(self.store, "fixture", self.app_id)["attempts"]
        self.assertEqual(len(history), 2)
        self.assertEqual(next(item for item in history if item["attempt_id"] == before["attempt_id"]), before)
        self.assertEqual(self.counts(), {"review":1, "submit":0})

    def test_worker_lost_receipt_and_restart_recovery_never_repeat_mock_submission(self):
        operation_id = self.queue(submit=True)
        with patch.dict("os.environ", self.env), patch("services.applications.mock_adapter._receipt", return_value=None):
            self.assertEqual(execute_operation(operation_id)["status"], "DEAD_LETTERED")
        self.assertEqual(self.store.application(self.app_id)["state"], "SUBMISSION_UNCONFIRMED")
        self.assertEqual(self.counts(), {"review":1, "submit":1})
        before = read_attempt_history(self.store, "fixture", self.app_id)
        self.assertEqual(before["attempts"][0]["state"], "SUBMISSION_UNCONFIRMED")
        self.reopen()
        with self.assertRaisesRegex(OperationActionError, "RECONCILIATION_REQUIRED"):
            retry_dead_letter(self.store, operation_id)
        app = create_app(self.store, self.origin, self.root, operation_mode="FIXTURE")
        with TestClient(app) as client, patch("services.applications.mock_adapter.sync_playwright",
                side_effect=AssertionError("Receipt recovery opened Chromium")):
            endpoint = f"/profiles/fixture/applications/{self.app_id}"
            response = client.post(endpoint + "/confirmation-check", json={"request_id":"reviewed-receipt-check"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["submission_attempted"])
            self.assertEqual(self.store.application(self.app_id)["state"], "CONFIRMED")
            self.assertEqual(client.get(endpoint + "/mock-runs/reviewed-browser-run").json()["state"], "DEAD_LETTERED")
            self.assertEqual(self.queue(submit=True), operation_id)
        self.assertEqual(read_attempt_history(self.store, "fixture", self.app_id), before)
        self.assertEqual(self.counts(), {"review":1, "submit":1})


if __name__ == "__main__":
    unittest.main()
