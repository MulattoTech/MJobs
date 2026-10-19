"""Durable user run admission: no browser, queue server, or external provider."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from cli import ui_e2e
from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import AttemptConflict
from services.applications.run_requests import queue_mock_run, read_mock_run, validate_run_operation
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationRunRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-run-request-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate(); self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.app_id = prepare(self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)

    def tearDown(self):
        self.store.engine.dispose(); self.temp.cleanup()

    def queue(self, request="reviewed-run-1", submit=False):
        return queue_mock_run(self.store, "fixture", self.app_id, submit=submit, request_id=request)

    def operation(self, identifier):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one())

    def test_duplicate_user_request_and_database_reopen_keep_one_operation(self):
        first = self.queue()
        second = self.queue()
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertFalse(first["replayed"]); self.assertTrue(second["replayed"])
        self.assertEqual(self.store.count("worker_operations"), 1)
        self.store.engine.dispose()
        reopened = Store(self.store.url)
        try:
            self.assertEqual(read_mock_run(reopened, "fixture", self.app_id, "reviewed-run-1")["operation_id"], first["operation_id"])
        finally:
            reopened.engine.dispose()

    def test_concurrent_double_click_is_one_atomic_admission(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.queue(), range(2)))
        self.assertEqual(results[0]["operation_id"], results[1]["operation_id"])
        self.assertEqual(sum(not row["replayed"] for row in results), 1)
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_request_id_cannot_switch_from_review_to_submission(self):
        self.queue()
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_REQUEST_REUSED"):
            self.queue(submit=True)
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_new_intent_cannot_compete_with_queued_run(self):
        self.queue()
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_IN_PROGRESS"):
            self.queue("second-intent")
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_terminal_request_replay_never_reopens_operation(self):
        for state in ("COMPLETED", "CANCELLED", "DEAD_LETTERED", "DISCARDED", "BLOCKED"):
            with self.subTest(state=state):
                request = "terminal-" + state
                first = self.queue(request)
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == first["operation_id"]).values(state=state))
                self.assertEqual(self.queue(request)["state"], state)
                self.assertEqual(self.operation(first["operation_id"])["state"], state)

    def test_explicit_new_intent_after_cancel_does_not_reuse_cancelled_row(self):
        first = self.queue()
        from workers.operations import request_cancel
        self.assertEqual(request_cancel(self.store, first["operation_id"])["state"], "CANCELLED")
        second = self.queue("new-after-cancel")
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(second["state"], "QUEUED")
        self.assertEqual(self.queue()["state"], "CANCELLED")

    def test_queued_input_change_is_rejected_before_execution(self):
        first = self.queue()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(m.applications.c.id == self.app_id).values(document_version="unreviewed-version"))
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_INPUT_CHANGED"):
            validate_run_operation(self.store, self.operation(first["operation_id"]))
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_REQUEST_REUSED"):
            self.queue()
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_route_rewrite_cannot_silently_change_a_reviewed_run(self):
        first = self.queue()
        app = self.store.application(self.app_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == app["route_id"]).values(url="http://127.0.0.1:1/changed"))
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_INPUT_CHANGED"):
            validate_run_operation(self.store, self.operation(first["operation_id"]))

    def test_request_identity_is_strict_bounded_and_not_normalized(self):
        for value in (None, "", " leading", "a b", "x" * 129, 17, "../escape", "💥"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_REQUEST_INVALID"):
                    self.queue(value)
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_mode_requires_a_real_boolean(self):
        for value in (1, "true", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_MODE_INVALID"):
                    self.queue(submit=value)

    def test_ambiguous_and_confirmed_applications_do_not_admit_new_browser_work(self):
        for state, code in (("SUBMITTING", "RECONCILIATION_REQUIRED"), ("SUBMISSION_UNCONFIRMED", "RECONCILIATION_REQUIRED"), ("CONFIRMED", "APPLICATION_ALREADY_CONFIRMED")):
            with self.subTest(state=state):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.applications).where(m.applications.c.id == self.app_id).values(state=state))
                with self.assertRaisesRegex(AttemptConflict, code):
                    self.queue()
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_api_admission_lookup_and_profile_scope_are_durable(self):
        app = create_app(self.store, "http://127.0.0.1:8090", self.root, operation_mode="FIXTURE")
        endpoint = f"/profiles/fixture/applications/{self.app_id}/mock-run"
        with TestClient(app) as client, patch("services.applications.mock_adapter.sync_playwright", side_effect=AssertionError("Admission opened a browser")):
            first = client.post(endpoint, json={"submit": False, "request_id": "api-run"})
            self.assertEqual(first.status_code, 202, first.text)
            self.assertEqual(client.post(endpoint, json={"submit": False, "request_id": "api-run"}).json()["operation_id"], first.json()["operation_id"])
            changed = client.post(endpoint, json={"submit": True, "request_id": "api-run"})
            self.assertEqual(changed.status_code, 409)
            self.assertEqual(changed.json()["detail"]["code"], "APPLICATION_RUN_REQUEST_REUSED")
            lookup = client.get(endpoint + "s/api-run")
            self.assertEqual(lookup.status_code, 200, lookup.text)
            self.assertEqual(lookup.json()["state"], "QUEUED")
            self.assertFalse(lookup.json()["external_submission_enabled"])
            self.assertEqual(client.get(endpoint.replace("/fixture/", "/other/") + "s/api-run").status_code, 404)
            self.assertEqual(client.post(endpoint.replace("/fixture/", "/other/"), json={"submit": False, "request_id": "other"}).status_code, 404)
            self.assertEqual(client.get(endpoint + "s/unknown").status_code, 404)
            self.assertEqual(client.post(endpoint, json={"submit": False, "request_id": "bad id"}).status_code, 422)
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_terminal_public_run_receipt_is_journaled_with_allowlisted_context(self):
        queued = self.queue(request="diagnostic-run")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == queued["operation_id"]).values(
                    state="DEAD_LETTERED", error="RETRY_LIMIT"))

        app = create_app(self.store, "http://127.0.0.1:8090", self.root, operation_mode="FIXTURE")
        endpoint = f"/profiles/fixture/applications/{self.app_id}/mock-runs/diagnostic-run"
        journal_path = self.root / "terminal-evidence.json"
        journal = ui_e2e.EvidenceJournal(
            journal_path,
            {"mode": "ONBOARDING_FIXTURE", "source": {"commit": "fixture-source"}},
        )
        journal.begin("Run dry-run")
        with TestClient(app) as client:
            response = client.get(endpoint)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["operation_id"], queued["operation_id"])
            self.assertEqual(response.json()["state"], "DEAD_LETTERED")
            self.assertEqual(response.json()["error_code"], "RETRY_LIMIT")

            with self.assertRaises(ui_e2e.MockRunTerminalError) as caught:
                ui_e2e.wait_for_mock_run(
                    lambda path: client.get(path).json(),
                    endpoint,
                    queued["operation_id"],
                    failure_context={
                        "application_id": self.app_id,
                        "request_id": "diagnostic-run",
                        "expected_profile_version": "a" * 64,
                        "expected_document_version": 3,
                        "application_state": "PREPARED",
                        "preflight_result": "PASS",
                        "preflight_is_current": False,
                        "submit": False,
                        "unapproved_answer": "excluded from diagnostics",
                    },
                )

        journal.fail(caught.exception)
        saved = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["overall_state"], "FAIL")
        self.assertEqual(saved["remaining_state"], "NOT_RUN")
        self.assertEqual(saved["checks"][-1], {"action": "Run dry-run", "result": "FAIL"})
        self.assertEqual(saved["failure"]["terminal_operation"], {
            "kind": "APPLY_MOCK",
            "state": "DEAD_LETTERED",
            "operation_id": queued["operation_id"],
            "error_code": "RETRY_LIMIT",
            "error_present": True,
            "error_redacted": False,
            "application_id": self.app_id,
            "request_id": "diagnostic-run",
            "expected_profile_version": "a" * 64,
            "expected_document_version": 3,
            "preflight_result": "PASS",
            "preflight_is_current": False,
            "submit": False,
            "application_read_state": "UNAVAILABLE",
        })
        self.assertNotIn("unapproved_answer", journal_path.read_text(encoding="utf-8"))

    def test_terminal_public_run_receipt_journals_owner_matched_current_application_read(self):
        queued = self.queue(request="current-read-run")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == queued["operation_id"]).values(
                    state="DEAD_LETTERED", error="RETRY_LIMIT"))

        app = create_app(self.store, "http://127.0.0.1:8090", self.root, operation_mode="FIXTURE")
        endpoint = f"/profiles/fixture/applications/{self.app_id}/mock-runs/current-read-run"
        journal_path = self.root / "current-read-evidence.json"
        journal = ui_e2e.EvidenceJournal(
            journal_path,
            {"mode": "ONBOARDING_FIXTURE", "source": {"commit": "fixture-source"}},
        )
        journal.begin("Run dry-run")
        with TestClient(app) as client:
            current_application = {}

            def read_current_application():
                response = client.get(f"/profiles/fixture/applications/{self.app_id}")
                self.assertEqual(response.status_code, 200, response.text)
                current_application.update(response.json())
                self.assertEqual(current_application["profile_id"], "fixture")
                self.assertEqual(current_application["id"], self.app_id)
                return ui_e2e._terminal_application_context(
                    current_application, profile_id="fixture", application_id=self.app_id)

            with self.assertRaises(ui_e2e.MockRunTerminalError) as caught:
                ui_e2e.wait_for_mock_run(
                    lambda path: client.get(path).json(),
                    endpoint,
                    queued["operation_id"],
                    failure_context={
                        "application_id": self.app_id,
                        "request_id": "current-read-run",
                        "application_state": "SUBMISSION_UNCONFIRMED",
                        "submit": False,
                    },
                    failure_context_reader=read_current_application,
                )

        journal.fail(caught.exception)
        terminal = json.loads(journal_path.read_text(encoding="utf-8"))["failure"]["terminal_operation"]
        self.assertEqual(terminal["application_read_state"], "CURRENT")
        self.assertEqual(terminal["application_state"], current_application["state"])
        self.assertEqual(terminal["current_profile_version"], current_application["profile_version"])
        self.assertEqual(terminal["current_document_version"], current_application["document_version"])
        self.assertNotEqual(terminal["application_state"], "SUBMISSION_UNCONFIRMED")

    def test_public_run_receipt_classifies_builtin_error_type_without_leaking_it(self):
        queued = self.queue(request="diagnostic-type-error", submit=True)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == queued["operation_id"]).values(
                    state="DEAD_LETTERED", error="TypeError"))

        app = create_app(self.store, "http://127.0.0.1:8090", self.root, operation_mode="FIXTURE")
        endpoint = f"/profiles/fixture/applications/{self.app_id}/mock-runs/diagnostic-type-error"
        journal_path = self.root / "type-error-evidence.json"
        journal = ui_e2e.EvidenceJournal(
            journal_path,
            {"mode": "ONBOARDING_FIXTURE", "source": {"commit": "fixture-source"}},
        )
        journal.begin("Submit to local mock")
        with TestClient(app) as client:
            receipt = client.get(endpoint)
            self.assertEqual(receipt.status_code, 200, receipt.text)
            self.assertEqual(receipt.json()["error_code"], "TypeError")
            with self.assertRaises(ui_e2e.MockRunTerminalError) as caught:
                ui_e2e.wait_for_mock_run(
                    lambda path: client.get(path).json(),
                    endpoint,
                    queued["operation_id"],
                    failure_context={
                        "application_id": self.app_id,
                        "request_id": "diagnostic-type-error",
                        "submit": True,
                    },
                )

        journal.fail(caught.exception)
        encoded = journal_path.read_text(encoding="utf-8")
        terminal = json.loads(encoded)["failure"]["terminal_operation"]
        self.assertEqual(terminal["error_category"], "BUILTIN_TYPE_ERROR")
        self.assertTrue(terminal["error_present"])
        self.assertTrue(terminal["error_redacted"])
        self.assertNotIn("error_code", terminal)
        self.assertNotIn("TypeError", encoded)
        self.assertEqual(json.loads(encoded)["overall_state"], "FAIL")
        self.assertEqual(json.loads(encoded)["remaining_state"], "NOT_RUN")

    def test_public_submit_request_runs_real_worker_against_local_mock_fixture(self):
        from workers.tasks import execute_operation

        with fixture_server() as origin:
            application = self.store.application(self.app_id)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.routes).where(
                    m.routes.c.id == application["route_id"],
                ).values(url=f"{origin}/ats/apply/{self.store.job(application['job_id'])['requisition']}"))

            app = create_app(self.store, origin, self.root, operation_mode="FIXTURE")
            endpoint = f"/profiles/fixture/applications/{self.app_id}/mock-run"
            with TestClient(app) as client:
                queued = client.post(endpoint, json={
                    "submit": True,
                    "request_id": "local-worker-diagnostic",
                })
                self.assertEqual(queued.status_code, 202, queued.text)
                operation_id = queued.json()["operation_id"]
                with patch.dict("os.environ", {
                    "MJOBS_DATABASE_URL": self.store.url,
                    "MJOBS_OUTPUT_DIR": str(self.root),
                    "MJOBS_MOCK_ORIGIN": origin,
                    "MJOBS_OPERATION_MODE": "FIXTURE",
                }):
                    worker_result = execute_operation(operation_id)
                self.assertEqual(worker_result["status"], "COMPLETED", worker_result)

                receipt = client.get(endpoint + "s/local-worker-diagnostic")
                self.assertEqual(receipt.status_code, 200, receipt.text)
                self.assertEqual(receipt.json()["state"], "COMPLETED")
                self.assertIsNone(receipt.json()["error_code"])
                self.assertFalse(receipt.json()["external_submission_enabled"])

    def test_personal_mode_rejects_explicit_admission_and_recovery(self):
        personal=Store(f"sqlite:///{(self.root / 'personal.db').as_posix()}")
        try:
            personal.migrate();personal.bind_workspace_mode("PERSONAL")
            with TestClient(create_app(personal,"http://127.0.0.1:8090",self.root,operation_mode="PERSONAL")) as client:
                endpoint=f"/profiles/fixture/applications/{self.app_id}/mock-run"
                self.assertEqual(client.post(endpoint,json={"submit":True,"request_id":"must-not-run"}).status_code,409)
                self.assertEqual(client.get(endpoint+"s/must-not-run").status_code,409)
            self.assertEqual(personal.count("worker_operations"),0)
        finally:
            personal.engine.dispose()


if __name__ == "__main__":
    unittest.main()
