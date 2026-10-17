"""Connected fixture snapshots can be preflighted and reread after restart."""
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.applications import controlled_dry_run as fixture_flow
from services.applications.adapters.workday import WorkdayApplicationAdapter
from services.applications.adapters.workday_uploads import preflight_workday_uploads
from tests.integration import test_controlled_application_dry_run as controlled_tests


class WorkdayFixtureUploadPreflightTests(unittest.TestCase):
    """Reuse the existing isolated SQLite application fixture without browser launch."""

    _new_store = controlled_tests.ControlledApplicationDryRunTests._new_store
    setUp = controlled_tests.ControlledApplicationDryRunTests.setUp
    tearDown = controlled_tests.ControlledApplicationDryRunTests.tearDown

    def _preflight_from_durable_documents(self):
        current = fixture_flow.preview(self.store, "fixture", self.application_id)
        prepared, _supporting = fixture_flow._snapshots(self.store, self.root, current)
        fields = [
            {"kind": "resume", "required": True, "ambiguous": False},
            {"kind": "cover-letter", "required": True, "ambiguous": False},
        ]
        return preflight_workday_uploads(fields, prepared)

    def test_prepared_documents_preflight_and_read_back_after_store_reopen(self):
        before = self._preflight_from_durable_documents()
        self.assertEqual([item["kind"] for item in before], ["resume", "cover-letter"])
        self.assertTrue(all(set(item) == {
            "kind", "document_id", "size_bytes", "sha256"} for item in before))

        self.client.close()
        self.store.engine.dispose()
        self.store = self._new_store()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))
        after = self._preflight_from_durable_documents()
        self.assertEqual(after, before)

        capabilities = WorkdayApplicationAdapter().capabilities()
        self.assertFalse(capabilities["document_upload"])
        self.assertFalse(capabilities["submission"])

    def test_ambiguous_or_interrupted_selection_fails_before_upload(self):
        from services.applications.upload_integrity import UploadIntegrityError

        current = fixture_flow.preview(self.store, "fixture", self.application_id)
        prepared, _supporting = fixture_flow._snapshots(self.store, self.root, current)
        with self.assertRaisesRegex(ValueError, "WORKDAY_UPLOAD_FIELDS_INVALID"):
            preflight_workday_uploads(
                [{"kind": "resume", "required": True, "ambiguous": True}], prepared[:1])
        with self.assertRaisesRegex(ValueError, "WORKDAY_UPLOAD_REQUIRED_MISSING"):
            preflight_workday_uploads(
                [
                    {"kind": "resume", "required": True, "ambiguous": False},
                    {"kind": "cover-letter", "required": True, "ambiguous": False},
                ], prepared[:1])

        with self.assertRaisesRegex(UploadIntegrityError, "UPLOAD_FILE_UNAVAILABLE"):
            fixture_flow.snapshot_prepared(
                {"kind": "resume", "id": "missing", "sha256": {"pdf": "0" * 64}},
                self.root / "missing-resume.pdf",
            )


if __name__ == "__main__":
    unittest.main()
