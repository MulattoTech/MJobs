"""Persisted application context for read-only reviews and local fixture runs."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_controlled_application_dry_run as flow
from workers.operations import recover_expired
from workers.structured_logging import log_operation_finished


class ApplicationLogFixture:
    _new_store = flow.ControlledApplicationDryRunTests._new_store
    setUp = flow.ControlledApplicationDryRunTests.setUp
    tearDown = flow.ControlledApplicationDryRunTests.tearDown
    path = flow.ControlledApplicationDryRunTests.path
    execute = flow.ControlledApplicationDryRunTests.execute
    review_and_queue = flow.ControlledApplicationDryRunTests.review_and_queue

    def environment(self):
        return {**os.environ, "MJOBS_OPERATION_MODE": "FIXTURE",
                "MJOBS_DATABASE_URL": self.database_url,
                "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
                "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
                "MJOBS_OUTPUT_DIR": str(self.root)}

    def assert_context(self, record, kind, result):
        application = self.store.application(self.application_id)
        with self.store.engine.connect() as connection:
            company_id = connection.execute(select(m.jobs.c.company_id).where(
                m.jobs.c.id == application["job_id"])).scalar_one()
        self.assertEqual(record["application_id"], self.application_id)
        self.assertEqual(record["job_id"], application["job_id"])
        self.assertEqual(record["company_id"], company_id)
        self.assertEqual(record["ats"], "greenhouse")
        self.assertEqual(record["operation_kind"], kind)
        self.assertEqual(record["result"], result)

    def assert_persisted_then_log(self, operation, result, error, duration):
        with self.store.engine.connect() as connection:
            state = connection.execute(select(m.operations.c.state).where(
                m.operations.c.id == operation["id"])).scalar_one()
        self.assertEqual(state, result)
        if result == "COMPLETED":
            request_id = operation["params"]["request_id"]
            if operation["kind"] == "APPLICATION_FORM_REVIEW":
                path = f"/profiles/fixture/applications/{self.application_id}/form-reviews/{request_id}"
                saved = self.client.get(path).json()
                self.assertEqual(saved["state"], "COMPLETED")
                self.assertIsNotNone(saved["analysis"])
            elif operation["kind"] == "APPLICATION_DRY_RUN":
                saved = self.client.get(self.path() + "/runs/" + request_id).json()
                self.assertEqual(saved["state"], "COMPLETED")
                self.assertIsNotNone(saved["receipt"])
        log_operation_finished(operation, result, error, duration)

    def reopen(self):
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def queue_review(self, request_id):
        path = f"/profiles/fixture/applications/{self.application_id}/form-reviews"
        queued = self.client.post(path, json={"request_id": request_id})
        self.assertEqual(queued.status_code, 202, queued.text)
        return path, queued.json()["operation_id"]

    def execute_review(self, operation_id):
        class LocalPublicClient:
            def __init__(inner, website, check=None, total_timeout=None):
                inner.website, inner.check = website, check

            def get(inner, url):
                inner.check()
                if url != inner.website:
                    raise AssertionError("Unexpected fixture fetch")
                return type("Response", (), {
                    "url": url, "headers": {"content-type": "text/html"},
                    "content": b"<form><input name='email' type='email'></form>",
                })()

            def allowed(inner, url):
                return url == inner.website

            def close(inner):
                pass

        with patch("workers.tasks.PublicCompanyClient", LocalPublicClient):
            return self.execute(operation_id)


class ApplicationOperationLogTests(ApplicationLogFixture, unittest.TestCase):
    def test_form_review_logs_after_persisted_result_and_survives_reopen(self):
        path, operation_id = self.queue_review("logged-review")
        with self.assertLogs("mjobs.operations", level="INFO") as captured, patch(
                "workers.tasks.log_operation_finished", side_effect=self.assert_persisted_then_log):
            result = self.execute_review(operation_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(len(captured.records), 1)
        record = json.loads(captured.records[0].getMessage())
        self.assert_context(record, "APPLICATION_FORM_REVIEW", "COMPLETED")
        saved = self.client.get(path + "/logged-review").json()
        self.assertEqual(saved["state"], "COMPLETED")
        self.assertIsNotNone(saved["analysis"])
        self.reopen()
        self.assertEqual(self.client.get(path + "/logged-review").json(), saved)
        with self.assertNoLogs("mjobs.operations", level="INFO"):
            self.assertEqual(self.execute_review(operation_id)["status"], "ALREADY_CLAIMED_OR_CAPACITY")

    def test_dry_run_blocked_then_fresh_intent_logs_persisted_context_and_receipt(self):
        preview, body, queued = self.review_and_queue("logged-blocked")
        application = self.store.application(self.application_id)
        with self.store.engine.connect() as connection:
            document = connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id, m.documents.c.kind == "resume",
            )).mappings().first()
        path = self.root / self.application_id / document["files"]["pdf"]
        original = path.read_bytes()
        path.write_bytes(b"%PDF-corrupted")
        try:
            with self.assertLogs("mjobs.operations", level="INFO") as captured, patch(
                    "workers.tasks.log_operation_finished", side_effect=self.assert_persisted_then_log):
                result = self.execute(queued["operation_id"])
            self.assertEqual(result["status"], "BLOCKED", result)
            record = json.loads(captured.records[0].getMessage())
            self.assert_context(record, "APPLICATION_DRY_RUN", "BLOCKED")
            self.assertEqual(record["error_code"], "CONTROLLED_DRY_RUN_DOCUMENT_CHANGED")
        finally:
            path.write_bytes(original)
        retry_body = {"request_id": "logged-repaired", "fingerprint": preview["fingerprint"],
                      "acknowledged": True}
        response = self.client.post(self.path() + "/runs/" + body["request_id"] + "/retry", json=retry_body)
        self.assertEqual(response.status_code, 202, response.text)
        retry = response.json()
        self.assertNotEqual(retry["operation_id"], queued["operation_id"])
        with self.assertLogs("mjobs.operations", level="INFO") as captured, patch(
                "workers.tasks.log_operation_finished", side_effect=self.assert_persisted_then_log):
            result = self.execute(retry["operation_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assert_context(json.loads(captured.records[0].getMessage()), "APPLICATION_DRY_RUN", "COMPLETED")
        self.reopen()
        blocked = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        saved = self.client.get(self.path() + "/runs/" + retry_body["request_id"]).json()
        self.assertEqual(blocked["state"], "BLOCKED")
        self.assertIsNone(blocked["receipt"])
        self.assertEqual(saved["state"], "COMPLETED")
        self.assertFalse(saved["receipt"]["provider_acceptance_claimed"])
        self.assertEqual(self.store.application(self.application_id), application)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_process_crash_receipt_recovery_logs_once_without_browser_replay(self):
        _, body, queued = self.review_and_queue("logged-crash")
        crash = """import os,sys
from workers import tasks
from workers.structured_logging import configure_operation_logger
configure_operation_logger()
original=tasks.execute_controlled_dry_run
def crash_after_receipt(*args,**kwargs):
    original(*args,**kwargs)
    os._exit(23)
tasks.execute_controlled_dry_run=crash_after_receipt
tasks.execute_operation(sys.argv[1])
"""
        process = subprocess.run([sys.executable, "-c", crash, queued["operation_id"]],
                                 env=self.environment(), capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 23, process.stderr)
        self.assertEqual(process.stdout, "")
        run_path = self.path() + "/runs/" + body["request_id"]
        receipt = self.client.get(run_path).json()["receipt"]
        self.assertIsNotNone(receipt)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == queued["operation_id"])
                               .values(lease_expires_at="2000-01-01T00:00:00+00:00"))
        recover_expired(self.store)
        resume = """import json,sys
from unittest.mock import patch
from workers.tasks import execute_operation
from workers.structured_logging import configure_operation_logger
configure_operation_logger()
with patch('services.applications.controlled_dry_run.run_browser',side_effect=AssertionError('must not replay')):
    print(json.dumps(execute_operation(sys.argv[1])))
    print(json.dumps(execute_operation(sys.argv[1])))
"""
        process = subprocess.run([sys.executable, "-c", resume, queued["operation_id"]],
                                 env=self.environment(), capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        records = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(records), 3)
        self.assert_context(records[0], "APPLICATION_DRY_RUN", "COMPLETED")
        self.assertEqual(records[1]["status"], "COMPLETED")
        self.assertEqual(records[2]["status"], "ALREADY_CLAIMED_OR_CAPACITY")
        self.reopen()
        self.assertEqual(self.client.get(run_path).json()["receipt"], receipt)


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"), "Requires isolated disposable PostgreSQL")
class ApplicationOperationLogPostgresTests(ApplicationOperationLogTests):
    _new_store = flow.ControlledApplicationDryRunPostgresTests._new_store
    _drop_schema = flow.ControlledApplicationDryRunPostgresTests._drop_schema
