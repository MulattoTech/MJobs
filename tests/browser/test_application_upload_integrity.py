"""Real browser byte verification and validation/use race acceptance."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications import mock_adapter, upload_integrity
from services.applications.attempt_history import read_attempt_history, AttemptConflict
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class UploadIntegrityBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-upload-integrity-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate(); self.store.bind_workspace_mode("FIXTURE"); self.store.save_profile(dict(PROFILE))
        self.fixture = fixture_server(); self.origin = self.fixture.__enter__()
        discover(self.store, self.origin, "fixture")
        self.app_id = prepare(self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        with self.store.engine.connect() as connection:
            self.documents = [dict(row) for row in connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.app_id)).mappings()]
        self.resume = next(row for row in self.documents if row["kind"] == "resume")
        self.resume_path = self.root / self.app_id / self.resume["files"]["pdf"]

    def tearDown(self):
        self.fixture.__exit__(None, None, None); self.store.engine.dispose(); self.temp.cleanup()

    def counts(self):
        return httpx.get(self.origin + "/_test/request-counts", trust_env=False).json()

    def test_all_selected_files_are_verified_and_manifest_survives_restart(self):
        client = TestClient(create_app(self.store, self.origin, self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        content = b"Fictional supporting document only."
        uploaded = client.post("/profiles/fixture/document-library", content=content,
            headers={"Content-Type": "text/plain", "X-Filename": "support.txt",
                     "X-Document-Kind": "SUPPORTING_DOCUMENT", "X-Document-Label": "Test support"})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        revision = uploaded.json()["revision_id"]
        selected = client.put(f"/profiles/fixture/applications/{self.app_id}/supporting-documents",
            json={"revision_ids": [revision], "request_id": "selected-files"})
        self.assertEqual(selected.status_code, 200, selected.text)
        result = apply_mock(self.store, self.app_id, self.origin, self.root, request_id="upload-proof")
        self.assertEqual(result["state"], "DRY_RUN_COMPLETE")
        manifest = read_attempt_history(self.store, "fixture", self.app_id)["attempts"][0]["verified_uploads"]
        self.assertEqual(len(manifest), 3)
        self.assertEqual({row["kind"] for row in manifest}, {"resume", "cover-letter", "supporting-document"})
        for row in self.documents:
            entry = next(item for item in manifest if item["document_id"] == row["id"])
            self.assertEqual(entry["sha256"], row["sha256"]["pdf"])
        entry = next(item for item in manifest if item["document_id"] == revision)
        self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
        received = httpx.get(self.origin + "/_test/supporting-uploads", trust_env=False).json()
        self.assertIn(hashlib.sha256(content).hexdigest(), str(received))
        self.assertEqual(self.counts(), {"review": 1, "submit": 0})
        reopened = Store(str(self.store.engine.url))
        try:
            self.assertEqual(read_attempt_history(reopened, "fixture", self.app_id)["attempts"][0]["verified_uploads"], manifest)
        finally:
            reopened.engine.dispose()
        # A different supporting-file selection cannot silently reuse the same
        # durable execution request identity.
        selected = client.put(f"/profiles/fixture/applications/{self.app_id}/supporting-documents",
            json={"revision_ids": [], "request_id": "clear-selection"})
        self.assertEqual(selected.status_code, 200, selected.text)
        with self.assertRaisesRegex(AttemptConflict, "REQUEST_REUSED"):
            apply_mock(self.store, self.app_id, self.origin, self.root, request_id="upload-proof")
        self.assertEqual(self.counts(), {"review": 1, "submit": 0})
        client.close()

    def test_file_changed_after_early_validation_is_rejected_before_browser(self):
        original = mock_adapter._preflight
        def change_after_validation(*args, **kwargs):
            result = original(*args, **kwargs)
            self.resume_path.write_bytes(b"%PDF-unverified replacement")
            return result
        with patch.object(mock_adapter, "_preflight", side_effect=change_after_validation), \
             patch.object(mock_adapter, "sync_playwright", side_effect=AssertionError("No browser allowed")):
            with self.assertRaisesRegex(upload_integrity.UploadIntegrityError, "CONTENT_CHANGED"):
                apply_mock(self.store, self.app_id, self.origin, self.root)
        self.assertEqual(self.store.count("application_attempts"), 0)
        self.assertEqual(self.counts(), {"review": 0, "submit": 0})

    def test_disk_changes_after_snapshot_cannot_change_browser_uploaded_bytes(self):
        original = upload_integrity.attach_verified_files
        def mutate_after_snapshot(locator, snapshots, **kwargs):
            if snapshots[0].kind == "resume":
                self.resume_path.write_bytes(b"%PDF-replaced after immutable snapshot")
            return original(locator, snapshots, **kwargs)
        with patch.object(upload_integrity, "attach_verified_files", side_effect=mutate_after_snapshot):
            self.assertEqual(apply_mock(self.store, self.app_id, self.origin, self.root)["state"], "DRY_RUN_COMPLETE")
        manifest = read_attempt_history(self.store, "fixture", self.app_id)["attempts"][0]["verified_uploads"]
        selected = next(row for row in manifest if row["kind"] == "resume")
        self.assertEqual(selected["sha256"], self.resume["sha256"]["pdf"])
        self.assertNotEqual(selected["sha256"], hashlib.sha256(self.resume_path.read_bytes()).hexdigest())
        self.assertEqual(self.counts(), {"review": 1, "submit": 0})

    def test_late_browser_file_replacement_is_blocked_before_review(self):
        original = upload_integrity.verify_file_selection
        resume_checks = 0
        def tamper_before_final_readback(locator, snapshots, **kwargs):
            nonlocal resume_checks
            if snapshots[0].kind == "resume":
                resume_checks += 1
                if resume_checks == 2:
                    locator.evaluate("""input => {
                        const changed = new DataTransfer();
                        changed.items.add(new File(['%PDF-tampered browser file'], input.files[0].name,
                            {type: 'application/pdf'}));
                        input.files = changed.files;
                    }""")
            return original(locator, snapshots, **kwargs)
        with patch.object(upload_integrity, "verify_file_selection", side_effect=tamper_before_final_readback):
            with self.assertRaisesRegex(upload_integrity.UploadIntegrityError, "BROWSER_BYTES_MISMATCH"):
                apply_mock(self.store, self.app_id, self.origin, self.root)
        self.assertEqual(resume_checks, 2)
        self.assertEqual(self.counts(), {"review": 0, "submit": 0})
        attempt = read_attempt_history(self.store, "fixture", self.app_id)["attempts"][0]
        self.assertEqual(attempt["state"], "REVIEW_REQUIRED")
        self.assertEqual(attempt["error_code"], "UPLOAD_INTEGRITY_FAILED")
        self.assertEqual(attempt["verified_uploads"], [])
