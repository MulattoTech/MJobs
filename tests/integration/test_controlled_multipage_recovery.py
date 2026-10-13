"""Crash and cancellation at a controlled multi-page boundary remain recoverable."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from playwright.sync_api import Locator
from sqlalchemy import select, update

from packages.persistence import models as m
from tests.integration import test_controlled_application_dry_run as contract
from workers.operations import OperationTimedOut, recover_expired


class ControlledDryRunMultipageRecoveryTests(unittest.TestCase):
    _new_store = contract.ControlledApplicationDryRunTests._new_store
    setUp = contract.ControlledApplicationDryRunTests.setUp
    tearDown = contract.ControlledApplicationDryRunTests.tearDown
    path = contract.ControlledApplicationDryRunTests.path
    execute = contract.ControlledApplicationDryRunTests.execute
    review_and_queue = contract.ControlledApplicationDryRunTests.review_and_queue

    def _worker_environment(self):
        return {
            **os.environ,
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root),
        }

    def _expire(self, operation_id):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id,
            ).values(lease_expires_at="2000-01-01T00:00:00+00:00"))

    def _operation(self, operation_id):
        with self.store.engine.connect() as connection:
            return connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one()

    def test_crash_after_contact_validation_recovers_from_page_one(self):
        _, body, queued = self.review_and_queue("multipage-crash")
        crash = r'''
import os, sys
from playwright.sync_api import Locator
from workers import tasks
original_click = Locator.click
def crash_before_documents(self, *args, **kwargs):
    if self.get_attribute("data-controlled-next") is not None:
        valid_contact_page = self.evaluate("""a => location.href ===
            "http://127.0.0.1:1/mjobs-controlled-application" &&
            a.closest("html").querySelector('form[data-controlled-page="contact"]').checkValidity()""")
        if not valid_contact_page:
            raise AssertionError("contact page was not validated before boundary")
        os._exit(29)
    return original_click(self, *args, **kwargs)
Locator.click = crash_before_documents
tasks.execute_operation(sys.argv[1])
'''
        result = subprocess.run(
            [sys.executable, "-c", crash, queued["operation_id"]],
            env=self._worker_environment(), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 29, result.stderr)
        run_path = self.path() + "/runs/" + body["request_id"]
        after_crash = self.client.get(run_path).json()
        self.assertEqual(after_crash["state"], "RUNNING")
        self.assertIsNone(after_crash["receipt"])
        self.assertEqual(self.store.count("application_attempts"), 0)
        self.assertEqual(self._operation(queued["operation_id"])["attempt_count"], 1)

        self._expire(queued["operation_id"])
        self.assertEqual(recover_expired(self.store), 1)
        resume = r'''
import json, sys
from playwright.sync_api import Locator
from workers.tasks import execute_operation
original_fill = Locator.fill
contact_fills = []
def track_contact(self, value, *args, **kwargs):
    if self.get_attribute("name") == "full_name":
        contact_fills.append(value)
    return original_fill(self, value, *args, **kwargs)
Locator.fill = track_contact
result = execute_operation(sys.argv[1])
print("RECOVERY_RESULT=" + json.dumps({"result": result, "contact_fills": contact_fills}))
'''
        result = subprocess.run(
            [sys.executable, "-c", resume, queued["operation_id"]],
            env=self._worker_environment(), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        saved_line = next((line for line in result.stdout.splitlines()
                           if line.startswith("RECOVERY_RESULT=")), None)
        self.assertIsNotNone(saved_line, result.stdout)
        resumed = json.loads(saved_line.removeprefix("RECOVERY_RESULT="))
        self.assertEqual(resumed["result"]["status"], "COMPLETED", resumed)
        self.assertEqual(len(resumed["contact_fills"]), 1, resumed)
        self.assertEqual(self._operation(queued["operation_id"])["attempt_count"], 2)

        completed = self.client.get(run_path).json()
        self.assertEqual([page["id"] for page in completed["receipt"]["pages"]],
                         ["contact", "documents"])
        self.assertEqual(len(completed["receipt"]["documents"]), 2)
        self.assertFalse(completed["receipt"]["submission_enabled"])
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_cancel_before_documents_navigation_saves_no_receipt_and_retry_is_fresh(self):
        preview, body, queued = self.review_and_queue("multipage-cancel")
        run_path = self.path() + "/runs/" + body["request_id"]
        original_click = Locator.click
        boundary = []

        def cancel_at_boundary(locator, *args, **kwargs):
            if locator.get_attribute("data-controlled-next") is not None:
                boundary.append(True)
                cancelled = self.client.post(run_path + "/cancel")
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertTrue(cancelled.json()["cancel_requested"])
            return original_click(locator, *args, **kwargs)

        with patch.object(Locator, "click", cancel_at_boundary):
            result = self.execute(queued["operation_id"])
        self.assertEqual(boundary, [True], "worker did not reach the page boundary")
        self.assertEqual(result["status"], "CANCELLED", result)
        first = self.client.get(run_path).json()
        self.assertEqual(first["state"], "CANCELLED")
        self.assertIsNone(first["receipt"])
        self.assertEqual(self.store.count("application_attempts"), 0)

        retry_body = {
            "request_id": "multipage-cancel-retry",
            "fingerprint": preview["fingerprint"],
            "acknowledged": True,
        }
        retry = self.client.post(run_path + "/retry", json=retry_body)
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertNotEqual(retry.json()["operation_id"], queued["operation_id"])
        self.assertEqual(self.execute(retry.json()["operation_id"])["status"], "COMPLETED")
        second = self.client.get(self.path() + "/runs/" + retry_body["request_id"]).json()
        self.assertEqual(second["retry_of"], body["request_id"])
        self.assertEqual([page["id"] for page in second["receipt"]["pages"]],
                         ["contact", "documents"])
        self.assertIsNone(self.client.get(run_path).json()["receipt"])
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_timeout_at_page_boundary_saves_no_partial_receipt(self):
        _, body, queued = self.review_and_queue("multipage-timeout")
        original_click = Locator.click
        boundary = []

        def timeout_at_boundary(locator, *args, **kwargs):
            if locator.get_attribute("data-controlled-next") is not None:
                boundary.append(True)
                raise OperationTimedOut("Synthetic deadline at controlled page boundary")
            return original_click(locator, *args, **kwargs)

        with patch.object(Locator, "click", timeout_at_boundary):
            result = self.execute(queued["operation_id"])
        self.assertEqual(boundary, [True], "worker did not reach the page boundary")
        self.assertEqual(result["status"], "DEAD_LETTERED", result)
        run = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertEqual(run["state"], "DEAD_LETTERED")
        self.assertEqual(run["error_code"], "OPERATION_TIMEOUT")
        self.assertIsNone(run["receipt"])
        self.assertEqual(self.store.count("application_attempts"), 0)


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Requires isolated disposable PostgreSQL")
class ControlledDryRunMultipageRecoveryPostgresTests(ControlledDryRunMultipageRecoveryTests):
    _new_store = contract.ControlledApplicationDryRunPostgresTests._new_store
    _drop_schema = contract.ControlledApplicationDryRunPostgresTests._drop_schema
