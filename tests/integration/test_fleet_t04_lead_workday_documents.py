"""Connected Workday document fixtures; no employer network or submission."""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from tests.integration import test_controlled_application_dry_run as _base


class WorkdayDocumentFlowTests(unittest.TestCase):
    _new_store = _base.ControlledApplicationDryRunTests._new_store
    tearDown = _base.ControlledApplicationDryRunTests.tearDown
    path = _base.ControlledApplicationDryRunTests.path
    execute = _base.ControlledApplicationDryRunTests.execute
    review_and_queue = _base.ControlledApplicationDryRunTests.review_and_queue

    def setUp(self):
        _base.ControlledApplicationDryRunTests.setUp(self)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(provider="workday", tenant="acme", requisition="R12345",
                     url="https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_R12345"))

    def _select(self):
        uploaded = self.client.post("/profiles/fixture/document-library",
            content=b"Synthetic supporting evidence",
            headers={"Content-Type": "text/plain", "X-Filename": "fixture.txt",
                     "X-Document-Kind": "SUPPORTING_DOCUMENT", "X-Document-Label": "Fixture"})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        revision = uploaded.json()
        response = self.client.put(self.path() + "/supporting-documents", json={
            "revision_ids": [revision["revision_id"]], "request_id": "selection"})
        self.assertEqual(response.status_code, 200, response.text)
        return revision

    def test_connected_browser_selection_receipt_reopen_and_replay(self):
        from services.applications.adapters.workday_uploads import preflight_workday_uploads
        revision = self._select()
        before = self.store.application(self.application_id)
        preview, body, queued = self.review_and_queue("workday-documents")
        self.assertEqual(preview["provider"], "workday")
        with patch("services.applications.adapters.workday_uploads.preflight_workday_uploads",
                   wraps=preflight_workday_uploads) as preflight:
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        preflight.assert_called_once()
        self.client.close()
        self.store.engine.dispose()
        self.store = self._new_store()
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        receipt = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertEqual(receipt, self.client.get(self.path() + "/runs/latest").json())
        proof = receipt["receipt"]
        self.assertEqual(proof["verification"], "BROWSER_SELECTED_BYTES")
        self.assertFalse(proof["provider_acceptance_claimed"])
        self.assertFalse(proof["submission_enabled"])
        self.assertEqual(len(proof["documents"]), 3)
        self.assertIn(revision["sha256"], [row["sha256"] for row in proof["documents"]])
        for row in proof["documents"]:
            self.assertEqual(set(row), {"kind", "document_id", "size_bytes", "sha256"})
        encoded = json.dumps(proof)
        self.assertNotIn("fixture.txt", encoded)
        self.assertNotIn("Synthetic supporting evidence", encoded)
        self.assertEqual(self.store.application(self.application_id), before)
        self.assertEqual(self.store.count("application_attempts"), 0)
        replay = self.client.post(self.path() + "/runs", json=body)
        self.assertEqual(replay.json()["operation_id"], queued["operation_id"])
        self.assertEqual(self.execute(queued["operation_id"])["status"], "ALREADY_CLAIMED_OR_CAPACITY")

    def test_preflight_rejection_blocks_browser_and_receipt(self):
        _, body, queued = self.review_and_queue("preflight-denied")
        with patch("services.applications.adapters.workday_uploads.preflight_workday_uploads",
                   side_effect=ValueError("WORKDAY_UPLOAD_SNAPSHOT_INVALID")), patch(
                   "services.applications.controlled_dry_run.run_browser") as browser:
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        browser.assert_not_called()
        self.assertIsNone(self.client.get(self.path() + "/runs/" + body["request_id"]).json()["receipt"])

    def test_workday_fixture_rejects_personal_workspace(self):
        # Mutate only this test's disposable database, never a personal workspace.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.workspace_identity).values(operation_mode="PERSONAL"))
        response = self.client.get(self.path())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "CONTROLLED_DRY_RUN_FIXTURE_ONLY")

    def test_archived_selection_blocks_before_browser(self):
        revision = self._select()
        _, body, queued = self.review_and_queue("archive-before-execution")
        archived = self.client.post(
            f"/profiles/fixture/document-library/{revision['revision_id']}/archive",
            headers={"Idempotency-Key": "archive-selected"})
        self.assertEqual(archived.status_code, 200, archived.text)
        with patch("services.applications.controlled_dry_run.run_browser") as browser:
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        browser.assert_not_called()
        self.assertIsNone(self.client.get(self.path() + "/runs/" + body["request_id"]).json()["receipt"])

    def test_cancelled_operation_keeps_selection_without_receipt(self):
        revision = self._select()
        _, body, queued = self.review_and_queue("cancel-documents")
        response = self.client.post(self.path() + "/runs/" + body["request_id"] + "/cancel")
        self.assertEqual(response.status_code, 200, response.text)
        with patch("services.applications.controlled_dry_run.run_browser") as browser:
            result = self.execute(queued["operation_id"])
        self.assertNotEqual(result["status"], "COMPLETED", result)
        browser.assert_not_called()
        self.assertIsNone(self.client.get(self.path() + "/runs/" + body["request_id"]).json()["receipt"])
        selected = self.client.get(self.path() + "/supporting-documents").json()
        self.assertEqual(selected["selected_revision_ids"], [revision["revision_id"]])

    def test_changed_prepared_bytes_block_before_browser(self):
        _, body, queued = self.review_and_queue("changed-document-bytes")
        with self.store.engine.connect() as connection:
            document = connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.kind == "resume")).mappings().first()
        path = self.root / self.application_id / document["files"]["pdf"]
        path.write_bytes(b"%PDF-synthetic changed after review")
        with patch("services.applications.controlled_dry_run.run_browser") as browser:
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        browser.assert_not_called()
        saved = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertEqual(saved["error_code"], "CONTROLLED_DRY_RUN_DOCUMENT_CHANGED")
        self.assertIsNone(saved["receipt"])

    def test_interruption_after_preflight_saves_no_partial_receipt(self):
        from workers.operations import OperationCancelled
        _, body, queued = self.review_and_queue("interrupted-documents")
        with patch("services.applications.controlled_dry_run.run_browser",
                   side_effect=OperationCancelled("synthetic interruption")):
            result = self.execute(queued["operation_id"])
        self.assertNotEqual(result["status"], "COMPLETED", result)
        saved = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertIsNone(saved["receipt"])

    def test_browser_manifest_must_match_preflight_before_persistence(self):
        _, body, queued = self.review_and_queue("manifest-mismatch")
        with patch("services.applications.controlled_dry_run.run_browser",
                   return_value={"documents": [], "pages": []}):
            result = self.execute(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED", result)
        saved = self.client.get(self.path() + "/runs/" + body["request_id"]).json()
        self.assertEqual(saved["error_code"], "CONTROLLED_DRY_RUN_DOCUMENT_CHANGED")
        self.assertIsNone(saved["receipt"])
