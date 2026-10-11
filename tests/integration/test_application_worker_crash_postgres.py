"""Real fixture-browser submission crash recovery against isolated PostgreSQL schemas."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from contextlib import contextmanager
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
import httpx
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
import uvicorn

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import read_attempt_history
from services.applications.run_requests import queue_mock_run, read_mock_run
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.operations import recover_expired
from workers.tasks import execute_operation


POSTGRES_URL_ENV = "MJOBS_APPLICATION_CRASH_POSTGRES_URL"
POSTGRES_URL = os.environ.get(POSTGRES_URL_ENV)
ROOT = Path(__file__).resolve().parents[2]
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@contextmanager
def receipt_outage_proxy(upstream, outage_file, outage_state):
    """Forward fixture traffic while serving an actual HTTP 503 for receipt reads on demand."""
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def forward(path: str, request: Request):
        if path.startswith("ats/receipts/") and outage_file.exists():
            outage_state["hits"] += 1
            return Response(status_code=503, content=b'{"detail":"fixture receipt temporarily unavailable"}',
                            media_type="application/json")
        # Keep the browser-visible proxy Host so the fixture's receipt URL is
        # allowlisted by MockClient at the proxy origin rather than its upstream.
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in {"content-length", "accept-encoding"}}
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.request(
                request.method, upstream.rstrip("/") + "/" + path,
                params=request.query_params, headers=headers, content=await request.body())
        excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        forwarded = {key: value for key, value in response.headers.items()
                     if key.lower() not in excluded}
        return Response(content=response.content, status_code=response.status_code,
                        headers=forwarded)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("Receipt outage proxy failed to start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Receipt outage proxy failed to stop")


@unittest.skipUnless(POSTGRES_URL, f"Set {POSTGRES_URL_ENV} to a disposable loopback mjobs_test_* database")
class PostgreSQLApplicationWorkerCrashTests(unittest.TestCase):
    """Crash only the owned worker child after the local ATS accepts one submit."""

    def setUp(self):
        self.base_url = make_url(POSTGRES_URL)
        if (not self.base_url.drivername.startswith("postgresql")
                or self.base_url.host not in LOOPBACK_HOSTS
                or not self.base_url.database
                or not self.base_url.database.startswith("mjobs_test_")):
            raise RuntimeError(
                f"{POSTGRES_URL_ENV} requires a loopback PostgreSQL database named mjobs_test_*")

        self.schema = "worker_crash_" + uuid4().hex
        self.admin_engine = create_engine(self.base_url, connect_args={"connect_timeout": 10})
        with self.admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.addCleanup(self.drop_schema)

        scoped_query = dict(self.base_url.query)
        scoped_query["options"] = f"-csearch_path={self.schema}"
        self.database_url = self.base_url.set(query=scoped_query).render_as_string(hide_password=False)
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-pg-app-crash-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture_context = fixture_server(receipt_file=self.root / "mock-receipts.json")
        self.upstream_origin = self.fixture_context.__enter__()
        self.addCleanup(self.close_fixture)
        self.receipt_outage_file = self.root / "receipt-outage"
        self.receipt_outage_state = {"hits": 0}
        self.proxy_context = receipt_outage_proxy(
            self.upstream_origin, self.receipt_outage_file, self.receipt_outage_state)
        self.origin = self.proxy_context.__enter__()
        self.addCleanup(self.close_proxy)
        self.store = Store(self.database_url)
        self.addCleanup(self.dispose_store)
        # Store.migrate is intentionally SQLite-only. These tests create only
        # the registered test metadata in their unique, disposable schema.
        from packages.persistence import resume_workspace  # noqa: F401
        m.metadata.create_all(self.store.engine)
        self.store.bind_workspace_mode("FIXTURE")
        self.profile_id = "pg-crash-" + uuid4().hex[:16]
        profile = dict(PROFILE, id=self.profile_id, email=f"{self.profile_id}@example.test")
        self.store.save_profile(profile)
        discover(self.store, self.origin, self.profile_id)
        job = self.store.jobs(self.profile_id)[0]
        self.application_id = prepare(self.store, job["id"], self.profile_id, self.root)
        self.worker_env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA")
            if key in os.environ
        }
        self.worker_env.update({
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_MOCK_ORIGIN": self.origin,
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "PYTHONUTF8": "1",
        })

    def close_fixture(self):
        if getattr(self, "fixture_context", None) is not None:
            self.fixture_context.__exit__(None, None, None)

    def close_proxy(self):
        if getattr(self, "proxy_context", None) is not None:
            self.proxy_context.__exit__(None, None, None)

    def dispose_store(self):
        if getattr(self, "store", None) is not None:
            self.store.engine.dispose()

    def drop_schema(self):
        if getattr(self, "admin_engine", None) is not None:
            try:
                with self.admin_engine.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
            finally:
                self.admin_engine.dispose()

    def queue_submit(self, request_id):
        operation = queue_mock_run(
            self.store, self.profile_id, self.application_id, submit=True, request_id=request_id)
        return operation["operation_id"]

    def operation(self, operation_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())

    def expire(self, operation_id):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(
                    lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))

    def reopen(self):
        self.store.engine.dispose()
        self.store = Store(self.database_url)

    def receipt(self):
        response = httpx.get(
            self.origin + f"/ats/receipts/{self.application_id}", timeout=5, trust_env=False)
        response.raise_for_status()
        return response.json()

    def counts(self):
        response = httpx.get(self.upstream_origin + "/_test/request-counts", timeout=5, trust_env=False)
        response.raise_for_status()
        return response.json()

    def crash_worker_after_provider_submit(self, operation_id):
        script = r'''
import os, sys
from services.applications import mock_adapter
def exit_after_provider_side_effect(*_args, **_kwargs):
    os._exit(73)
mock_adapter.record_mock_application_submitted = exit_after_provider_side_effect
from workers.tasks import execute_operation
execute_operation(sys.argv[1])
os._exit(74)
'''
        result = subprocess.run(
            [sys.executable, "-c", script, operation_id], cwd=ROOT,
            env=self.worker_env, capture_output=True, text=True, timeout=150,
        )
        self.assertEqual(result.returncode, 73,
                         "owned worker did not exit at the post-submit/pre-ack crash seam")

    def test_post_submit_worker_crash_recovers_from_receipt_without_second_browser_or_submit(self):
        request_id = "pg-submit-crash-" + uuid4().hex[:12]
        operation_id = self.queue_submit(request_id)
        self.crash_worker_after_provider_submit(operation_id)

        self.assertEqual(self.operation(operation_id)["state"], "RUNNING")
        self.assertEqual(self.operation(operation_id)["attempt_count"], 1)
        self.assertEqual(self.store.application(self.application_id)["state"], "SUBMITTING")
        attempts_before_recovery = read_attempt_history(
            self.store, self.profile_id, self.application_id)
        self.assertEqual(len(attempts_before_recovery["attempts"]), 1)
        self.assertEqual(attempts_before_recovery["attempts"][0]["state"], "RUNNING")
        self.assertEqual(attempts_before_recovery["attempts"][0]["current_step"], "SUBMIT")
        receipt = self.receipt()
        self.assertEqual(receipt["application_id"], self.application_id)
        self.assertEqual(receipt["submission_count"], 1)
        before_counts = self.counts()
        self.assertEqual(before_counts, {"review": 1, "submit": 1})

        self.reopen()
        self.expire(operation_id)
        self.assertEqual(recover_expired(self.store), 1)
        with patch.dict("os.environ", self.worker_env), patch(
                "services.applications.mock_adapter.sync_playwright",
                side_effect=AssertionError("Receipt recovery must not relaunch Chromium")):
            result = execute_operation(operation_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.operation(operation_id)["state"], "COMPLETED")
        self.assertEqual(self.operation(operation_id)["attempt_count"], 2)
        application = self.store.application(self.application_id)
        self.assertEqual(application["state"], "CONFIRMED")
        self.assertEqual(application["confirmation"]["receipt"], receipt["receipt"])
        self.assertEqual(read_attempt_history(
            self.store, self.profile_id, self.application_id), attempts_before_recovery)
        app = create_app(self.store, self.origin, self.root, operation_mode="FIXTURE")
        endpoint = f"/profiles/{self.profile_id}/applications/{self.application_id}/confirmation-check"
        with TestClient(app) as client, patch(
                "services.applications.mock_adapter.sync_playwright",
                side_effect=AssertionError("Post-recovery confirmation check must not open Chromium")):
            response = client.post(endpoint, json={"request_id": "pg-confirmed-check-" + uuid4().hex[:12]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["provider_receipt_verified"])
        self.assertFalse(response.json()["browser_started"])
        self.assertFalse(response.json()["submission_attempted"])
        self.assertEqual(self.receipt()["submission_count"], 1)
        self.assertEqual(self.counts(), before_counts)

        replay = queue_mock_run(
            self.store, self.profile_id, self.application_id, submit=True, request_id=request_id)
        self.assertEqual(replay["operation_id"], operation_id)
        self.assertTrue(replay["replayed"])
        self.assertEqual(read_mock_run(self.store, self.profile_id, self.application_id, request_id)["operation_id"],
                         operation_id)
        with self.assertRaisesRegex(ValueError, "APPLICATION_ALREADY_CONFIRMED"):
            queue_mock_run(self.store, self.profile_id, self.application_id,
                           submit=True, request_id=request_id + "-new")
        with self.assertRaisesRegex(ValueError, "APPLICATION_RUN_REQUEST_REUSED"):
            queue_mock_run(self.store, self.profile_id, self.application_id,
                           submit=False, request_id=request_id)
        self.assertEqual(self.receipt()["submission_count"], 1)
        self.assertEqual(self.counts(), before_counts)

    def test_ambiguous_post_crash_receipt_check_resolves_without_browser_or_resubmit(self):
        request_id = "pg-ambiguous-crash-" + uuid4().hex[:12]
        operation_id = self.queue_submit(request_id)
        self.crash_worker_after_provider_submit(operation_id)
        self.assertEqual(self.receipt()["submission_count"], 1)
        before_counts = self.counts()
        self.assertEqual(before_counts, {"review": 1, "submit": 1})
        attempts_before_recovery = read_attempt_history(
            self.store, self.profile_id, self.application_id)
        self.assertEqual(len(attempts_before_recovery["attempts"]), 1)
        self.assertEqual(attempts_before_recovery["attempts"][0]["state"], "RUNNING")
        self.assertEqual(attempts_before_recovery["attempts"][0]["current_step"], "SUBMIT")

        self.reopen()
        self.expire(operation_id)
        self.assertEqual(recover_expired(self.store), 1)
        self.receipt_outage_file.touch()
        with patch.dict("os.environ", self.worker_env), \
                patch("services.applications.mock_adapter.sync_playwright",
                      side_effect=AssertionError("Ambiguous receipt recovery must not open Chromium")):
            result = execute_operation(operation_id)
        self.receipt_outage_file.unlink()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.receipt_outage_state["hits"], 1)
        self.assertEqual(self.operation(operation_id)["state"], "BLOCKED")
        self.assertEqual(self.store.application(self.application_id)["state"], "SUBMISSION_UNCONFIRMED")
        self.assertEqual(self.receipt()["submission_count"], 1)
        self.assertEqual(self.counts(), before_counts)
        self.assertEqual(read_attempt_history(
            self.store, self.profile_id, self.application_id), attempts_before_recovery)

        retry = queue_mock_run(
            self.store, self.profile_id, self.application_id, submit=True, request_id=request_id)
        self.assertEqual(retry["operation_id"], operation_id)
        with self.assertRaisesRegex(ValueError, "RECONCILIATION_REQUIRED"):
            queue_mock_run(self.store, self.profile_id, self.application_id,
                           submit=True, request_id=request_id + "-new")
        app = create_app(self.store, self.origin, self.root, operation_mode="FIXTURE")
        endpoint = f"/profiles/{self.profile_id}/applications/{self.application_id}/confirmation-check"
        with TestClient(app) as client, patch(
                "services.applications.mock_adapter.sync_playwright",
                side_effect=AssertionError("Receipt-only confirmation check must not open Chromium")):
            response = client.post(endpoint, json={"request_id": "pg-receipt-check-" + uuid4().hex[:12]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "CONFIRMED")
        self.assertTrue(response.json()["provider_receipt_verified"])
        self.assertFalse(response.json()["browser_started"])
        self.assertFalse(response.json()["submission_attempted"])
        self.assertEqual(self.store.application(self.application_id)["state"], "CONFIRMED")
        self.assertEqual(read_attempt_history(
            self.store, self.profile_id, self.application_id), attempts_before_recovery)
        self.assertEqual(self.receipt()["submission_count"], 1)
        self.assertEqual(self.counts(), before_counts)


if __name__ == "__main__":
    unittest.main()
