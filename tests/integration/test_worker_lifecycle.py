"""Durable operation ownership, cancellation and expiry contracts."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from fixtures.mock_server import fixture_server
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation
from workers.operations import claim, request_cancel, recover_expired, check_cancelled, OperationCancelled
from workers.tasks import execute_operation


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def read(self, identifier):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one())

    def expired(self, identifier):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(lease_expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()))

    def test_capacity_is_independent_and_a_duplicate_cannot_claim(self):
        first = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        other = enqueue_operation(self.store, "DISCOVER", "other", {})
        over_capacity = enqueue_operation(self.store, "DISCOVER", "third", {})
        document = enqueue_operation(self.store, "PREPARE", "fixture", {"job_id": "unused"})
        self.assertIsNotNone(claim(self.store, first))
        self.assertIsNone(claim(self.store, first))
        self.assertIsNotNone(claim(self.store, other))
        self.assertIsNone(claim(self.store, over_capacity))
        self.assertIsNotNone(claim(self.store, document))

    def test_company_discovery_kinds_share_one_category_capacity(self):
        refresh_a = enqueue_operation(self.store, "COMPANY_SOURCE_REFRESH", "source-a", {})
        refresh_b = enqueue_operation(self.store, "COMPANY_SOURCE_REFRESH", "source-b", {})
        discovery = enqueue_operation(self.store, "COMPANY_DISCOVER", "company-c", {})
        document = enqueue_operation(self.store, "PREPARE", "application-d", {})
        application = enqueue_operation(self.store, "APPLY_MOCK", "application-e", {})
        settings = {
            "MJOBS_SOURCE_COLLECTION_CONCURRENCY": "2",
            "MJOBS_COMPANY_DISCOVERY_CONCURRENCY": "2",
            "MJOBS_AI_CONCURRENCY": "1",
            "MJOBS_DOCUMENT_GENERATION_CONCURRENCY": "1",
            "MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "1",
        }
        with patch.dict("os.environ", settings):
            self.assertIsNotNone(claim(self.store, refresh_a))
            self.assertIsNotNone(claim(self.store, refresh_b))
            self.assertIsNone(claim(self.store, discovery))
            self.assertIsNotNone(claim(self.store, document))
            self.assertIsNotNone(claim(self.store, application))

    def test_discovery_expiry_retries_at_most_three_claims(self):
        identifier = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        for attempt in range(3):
            self.assertIsNotNone(claim(self.store, identifier))
            self.expired(identifier)
            recover_expired(self.store)
            self.assertEqual(self.read(identifier)["state"], "QUEUED" if attempt < 2 else "FAILED")
        self.assertEqual(self.read(identifier)["attempt_count"], 3)

    def test_cancel_before_claim_and_running_cancellation_are_durable(self):
        identifier = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        self.assertEqual(request_cancel(self.store, identifier)["state"], "CANCELLED")
        self.assertIsNone(claim(self.store, identifier))
        other = enqueue_operation(self.store, "DISCOVER", "running", {})
        claim(self.store, other)
        self.assertTrue(request_cancel(self.store, other)["cancel_requested"])
        with self.assertRaises(OperationCancelled):
            check_cancelled(self.store, other)

    def test_over_budget_worker_is_not_recorded_completed(self):
        operation = enqueue_operation(self.store, "DISCOVER", "fixture", {"rescore_only": True})
        def slow_recalculate(*_args, **_kwargs):
            time.sleep(0.02)

        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                        MJOBS_MOCK_ORIGIN="http://127.0.0.1:8090", MJOBS_OUTPUT_DIR=str(self.root),
                        MJOBS_OPERATION_MODE="FIXTURE"), \
                patch("workers.tasks.recalculate", side_effect=slow_recalculate), \
                patch("workers.tasks.timeout", return_value=0.01), \
                patch("workers.tasks.check_cancelled"):
            result = execute_operation(operation)

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_category"], "OPERATION_TIMEOUT")
        saved = self.read(operation)
        self.assertEqual(saved["state"], "FAILED")
        self.assertEqual(saved["error"], "OPERATION_TIMEOUT")

    def test_reclaimed_attempt_fences_original_worker_before_side_effect(self):
        operation = enqueue_operation(self.store, "DISCOVER", "fixture", {"rescore_only": True})
        entered = threading.Event()
        resume = threading.Event()
        side_effects = []
        result = []

        def paused_recalculate(_store, _profile_id, *, check):
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("test did not release the stale worker")
            check()
            side_effects.append("published")

        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                        MJOBS_MOCK_ORIGIN="http://127.0.0.1:8090",
                        MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="FIXTURE"), \
                patch("workers.tasks.recalculate", side_effect=paused_recalculate):
            worker = threading.Thread(target=lambda: result.append(execute_operation(operation)), daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(5), "worker did not enter its operation")
                first_attempt = self.read(operation)["attempt_count"]
                self.expired(operation)
                self.assertEqual(recover_expired(self.store), 1)
                replacement = claim(self.store, operation)
                self.assertEqual(replacement["attempt_count"], first_attempt + 1)
            finally:
                resume.set()
                worker.join(5)

        self.assertFalse(worker.is_alive(), "worker did not stop after losing its claim")
        self.assertEqual(result[0]["status"], "OWNERSHIP_EXPIRED")
        self.assertEqual(side_effects, [])
        saved = self.read(operation)
        self.assertEqual(saved["state"], "RUNNING")
        self.assertEqual(saved["attempt_count"], first_attempt + 1)

    def test_product_worker_process_crash_recovers_from_durable_lease(self):
        operation = enqueue_operation(self.store, "DISCOVER", "fixture", {"rescore_only": True})
        marker = self.root / "worker-entered-operation.marker"
        script = """
import json, sys, time
from pathlib import Path
from workers import tasks
operation_id, marker_path, mode = sys.argv[1:]
if mode == "crash":
    def block_until_killed(*_args, **_kwargs):
        Path(marker_path).write_text("entered", encoding="utf-8")
        while True:
            time.sleep(0.05)
    tasks.recalculate = block_until_killed
print(json.dumps(tasks.execute_operation(operation_id)), flush=True)
"""
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        environment.update({
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:8090",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
        })
        project_root = Path(__file__).resolve().parents[2]
        crashed_worker = subprocess.Popen(
            [sys.executable, "-c", script, operation, str(marker), "crash"],
            cwd=project_root, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                if crashed_worker.poll() is not None:
                    stdout, stderr = crashed_worker.communicate()
                    self.fail(f"worker exited before crash point: {stdout} {stderr}")
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "worker did not reach the crash point")
            self.assertEqual(self.read(operation)["state"], "RUNNING")
            crashed_worker.kill()
            crashed_worker.communicate(timeout=5)
        finally:
            if crashed_worker.poll() is None:
                crashed_worker.kill()
                crashed_worker.communicate(timeout=5)

        self.expired(operation)
        self.assertEqual(recover_expired(self.store), 1)
        self.assertEqual(self.read(operation)["state"], "QUEUED")
        completed_worker = subprocess.run(
            [sys.executable, "-c", script, operation, str(marker), "resume"],
            cwd=project_root, env=environment, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(completed_worker.returncode, 0, completed_worker.stderr)
        self.assertEqual(json.loads(completed_worker.stdout)["status"], "COMPLETED")
        self.assertEqual(self.read(operation)["state"], "COMPLETED")

    def test_application_worker_exposes_its_remaining_monotonic_budget(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        application = prepare(self.store, self.store.jobs()[0]["id"], "fixture", self.root)
        operation = enqueue_operation(self.store, "APPLY_MOCK", application, {"submit": True})
        observed = []

        def apply_with_budget(_store, _application_id, _origin, _output_dir, *, check, **_kwargs):
            remaining = check.remaining_seconds()
            self.assertGreater(remaining, 0)
            self.assertLessEqual(remaining, 180)
            check()
            observed.append(remaining)
            return {"state": "DRY_RUN_COMPLETE"}

        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                        MJOBS_MOCK_ORIGIN="http://127.0.0.1:8090", MJOBS_OUTPUT_DIR=str(self.root),
                        MJOBS_OPERATION_MODE="FIXTURE"), \
                patch("workers.tasks.apply_mock", side_effect=apply_with_budget):
            result = execute_operation(operation)

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(observed), 1)

    def application_operation(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        application = prepare(self.store, self.store.jobs()[0]["id"], "fixture", self.root)
        operation = enqueue_operation(self.store, "APPLY_MOCK", application, {"submit": True})
        claim(self.store, operation)
        return application, operation

    def test_pre_submit_crash_stops_for_review(self):
        application, operation = self.application_operation()
        self.store.transition(application, ["PREPARED"], "FILLING")
        self.expired(operation)
        recover_expired(self.store)
        self.assertEqual(self.store.application(application)["state"], "REVIEW_REQUIRED")
        self.assertEqual(self.read(operation)["state"], "BLOCKED")

    def test_reserved_submission_only_reconciles_and_cannot_cancel(self):
        application, operation = self.application_operation()
        self.store.transition(application, ["PREPARED"], "SUBMITTING")
        result = request_cancel(self.store, operation)
        self.assertEqual(result["reason"], "RECONCILIATION_REQUIRED")
        self.assertFalse(result["cancel_requested"])
        self.expired(operation)
        recover_expired(self.store)
        self.assertEqual(self.store.application(application)["state"], "SUBMITTING")
        self.assertEqual(self.read(operation)["state"], "QUEUED")

    def test_recovered_application_without_receipt_is_explicitly_blocked(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            application = prepare(self.store, self.store.jobs()[0]["id"], "fixture", self.root)
            self.store.transition(application, ["PREPARED"], "SUBMITTING")
            operation = enqueue_operation(self.store, "APPLY_MOCK", application, {"submit": True})
            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url, MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="FIXTURE"):
                result = execute_operation(operation)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.read(operation)["error"], "SUBMISSION_UNCONFIRMED")
        self.assertEqual(self.store.application(application)["state"], "SUBMISSION_UNCONFIRMED")

    def test_personal_worker_blocks_stale_fixture_submission_before_browser(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            application = prepare(self.store, self.store.jobs()[0]["id"], "fixture", self.root)
            operation = enqueue_operation(self.store, "APPLY_MOCK", application, {"submit": True})
            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url, MJOBS_MOCK_ORIGIN=origin,
                            MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="PERSONAL"), \
                    patch("workers.tasks.apply_mock", side_effect=AssertionError("browser was called")):
                with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_MISMATCH"):
                    execute_operation(operation)
        self.assertEqual(self.read(operation)["state"], "QUEUED")
        self.assertEqual(self.read(operation)["attempt_count"], 0)
        self.assertEqual(self.store.application(application)["state"], "PREPARED")
        self.assertIsNone(self.store.application(application)["submitted_at"])

    def test_personal_worker_blocks_stale_fixture_discovery(self):
        operation = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url, MJOBS_MOCK_ORIGIN="http://127.0.0.1:8090",
                        MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="PERSONAL"), \
                patch("workers.tasks.discover", side_effect=AssertionError("fixture was called")):
            with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_MISMATCH"):
                execute_operation(operation)
        self.assertEqual(self.read(operation)["state"], "QUEUED")
        self.assertEqual(self.read(operation)["attempt_count"], 0)

    def test_personal_worker_blocks_stale_fixture_preparation(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        operation = enqueue_operation(self.store, "PREPARE", "fixture",
                                      {"job_id": self.store.jobs()[0]["id"]})
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url, MJOBS_MOCK_ORIGIN="http://127.0.0.1:8090",
                        MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="PERSONAL"), \
                patch("workers.tasks.prepare", side_effect=AssertionError("fixture preparation was called")):
            with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_MISMATCH"):
                execute_operation(operation)
        self.assertEqual(self.read(operation)["state"], "QUEUED")
        self.assertEqual(self.read(operation)["attempt_count"], 0)

    def test_cancelled_running_browser_never_reserves_submission(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            application = prepare(self.store, self.store.jobs()[0]["id"], "fixture", self.root)
            operation = enqueue_operation(self.store, "APPLY_MOCK", application, {"submit": True})
            def cancel_in_form(store, identifier, attempt_count=None):
                if store.application(application)["state"] == "FILLING":
                    request_cancel(store, identifier)
                check_cancelled(store, identifier, attempt_count)
            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url, MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="FIXTURE"), patch("workers.tasks.check_cancelled", side_effect=cancel_in_form):
                result = execute_operation(operation)
        self.assertEqual(result["status"], "CANCELLED")
        saved = self.store.application(application)
        self.assertEqual(saved["state"], "REVIEW_REQUIRED")
        self.assertNotIn("SUBMIT", saved["trace"])
        self.assertIsNone(saved["submitted_at"])
