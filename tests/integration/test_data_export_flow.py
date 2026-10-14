"""Profile-scoped downloads connected to durable export services."""
import base64
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.data_export import create_data_export_router
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


_BACKUP_SECTIONS = {
    "schema_version", "export_type", "profile_id", "profile", "profile_versions", "facts",
    "settings", "career_stories", "ai_evaluation_runs", "ai_job_evaluations",
    "ai_document_reviews", "source_candidates", "source_candidate_reviews",
    "company_discovery_requests", "jobs", "job_matches", "applications",
    "application_routes", "application_route_choices", "documents", "document_files",
    "attempts", "resume_revisions", "uploaded_resumes", "resume_workspace_revisions",
    "interview_prep_packs",
}
_PRIVATE_KEY_PARTS = (
    "password", "credential", "secret", "token", "cookie", "browser", "session",
    "raw", "diagnostic", "trace", "confirmation", "steps", "generationcontext",
    "path", "filepath",
)


class DataExportFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'exports.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(copy.deepcopy(PROFILE))
        other = copy.deepcopy(PROFILE)
        other.update(id="other-profile", name="Other Synthetic", email="other@example.test")
        self.store.save_profile(other)

        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
        self.job_id = job["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        self.store.save_provider_credential(
            "fixture", "openai", b"SYNTHETIC_EXPORT_API_SECRET",
        )
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id="synthetic-session-operation", kind="APPLY_MOCK",
                entity_id=self.application_id,
                params={"browser_session": "SYNTHETIC_EXPORT_SESSION"},
                state="QUEUED", error=None, updated_at="2026-10-11T00:00:00+00:00",
                attempt_count=0, cancel_requested=False,
            ))

        app = FastAPI()
        app.include_router(create_data_export_router(self.store, output_dir=self.root))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_csv_exports_are_profile_scoped_attachments(self):
        jobs = self.client.get("/profiles/fixture/exports/jobs.csv")
        applications = self.client.get("/profiles/other-profile/exports/applications.csv")

        self.assertEqual(jobs.status_code, 200, jobs.text)
        self.assertEqual(jobs.headers["content-type"], "text/csv; charset=utf-8")
        self.assertEqual(jobs.headers["content-disposition"], 'attachment; filename="mjobs-jobs.csv"')
        job_rows = list(csv.DictReader(io.StringIO(jobs.text)))
        self.assertTrue(job_rows)
        self.assertIn(self.job_id, {row["id"] for row in job_rows})

        self.assertEqual(applications.status_code, 200, applications.text)
        self.assertEqual(applications.headers["content-type"], "text/csv; charset=utf-8")
        self.assertEqual(applications.headers["content-disposition"],
                         'attachment; filename="mjobs-applications.csv"')
        application_rows = list(csv.DictReader(io.StringIO(applications.text)))
        self.assertEqual(application_rows, [])
        self.assertNotIn(self.application_id, applications.text)

    def test_json_export_reads_back_persisted_profile_jobs_and_applications(self):
        response = self.client.get("/profiles/fixture/exports/profile.json")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["content-disposition"],
                         'attachment; filename="mjobs-profile.json"')
        exported = response.json()
        self.assertEqual(exported["profile_id"], "fixture")
        self.assertEqual(exported["profile"]["email"], "alex@example.test")
        self.assertIn(self.job_id, {row["id"] for row in exported["jobs"]})
        self.assertIn(self.application_id,
                      {row["id"] for row in exported["applications"]})

    def test_complete_backup_contains_verified_document_bytes_without_private_state(self):
        response = self.client.get("/profiles/fixture/exports/backup.json")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["content-disposition"],
                         'attachment; filename="mjobs-profile-backup.json"')
        payload = response.json()
        self.assertEqual(set(payload), _BACKUP_SECTIONS)
        self.assertEqual(payload["export_type"], "profile_backup")
        self.assertEqual(payload["profile_id"], "fixture")
        self.assertEqual(payload["profile"]["id"], "fixture")
        self.assertIn(self.job_id, {row["id"] for row in payload["jobs"]})
        self.assertIn(self.application_id,
                      {row["id"] for row in payload["applications"]})
        self.assertTrue(payload["document_files"])
        for record in payload["document_files"]:
            content = base64.b64decode(record["content_base64"], validate=True)
            self.assertEqual(len(content), record["size_bytes"])
            self.assertEqual(hashlib.sha256(content).hexdigest(), record["sha256"])

        serialized = response.text.casefold()
        for marker in (
            "synthetic_export_api_secret", "synthetic_export_session",
            "credential_ciphertext", "browser_profile", "session_token",
        ):
            self.assertNotIn(marker, serialized)

        def assert_no_private_keys(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    compact_key = "".join(character for character in str(key).casefold()
                                          if character.isalnum())
                    self.assertFalse(any(part in compact_key for part in _PRIVATE_KEY_PARTS), key)
                    assert_no_private_keys(nested)
            elif isinstance(value, list):
                for nested in value:
                    assert_no_private_keys(nested)

        assert_no_private_keys(payload)

    def test_missing_profile_is_rejected_before_export(self):
        response = self.client.get("/profiles/not-a-profile/exports/jobs.csv")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "DATA_EXPORT_PROFILE_NOT_FOUND")

    def test_download_redacts_nested_auth_fields_after_database_reopen(self):
        answers = [{"metadata": {"api_key": "SYNTHETIC_API_KEY_VALUE",
                                 "Authorization": "SYNTHETIC_AUTH_HEADER_VALUE",
                                 "work_authorization": "Synthetic work answer"}}]
        with self.store.engine.begin() as connection:
            connection.execute(m.profile_versions.update().where(
                m.profile_versions.c.profile_id == "fixture",
            ).values(answers=answers))
        self.store.engine.dispose()
        for kind in ("profile.json", "backup.json"):
            response = self.client.get(f"/profiles/fixture/exports/{kind}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("SYNTHETIC_API_KEY_VALUE", response.text)
            self.assertNotIn("SYNTHETIC_AUTH_HEADER_VALUE", response.text)
            self.assertIn("Synthetic work answer", response.text)
        with self.store.engine.connect() as connection:
            persisted = connection.execute(m.profile_versions.select().where(
                m.profile_versions.c.profile_id == "fixture",
            )).mappings().first()["answers"]
        self.assertEqual(persisted, answers)

    def test_json_service_scope_mismatch_fails_closed(self):
        with patch("services.data_export.export_profile_json", return_value=json.dumps({
            "schema_version": 1,
            "export_type": "profile_json_export",
            "profile_id": "other-profile",
            "profile": {"id": "other-profile"},
        })):
            response = self.client.get("/profiles/fixture/exports/profile.json")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["code"], "DATA_EXPORT_RESPONSE_INVALID")
        self.assertNotIn("other-profile", response.text)

    def test_growing_backup_file_returns_fixed_error_and_safe_retry(self):
        from services.data_export import MAX_EXPORT_BYTES

        with self.store.engine.connect() as connection:
            document = connection.execute(m.documents.select().where(
                m.documents.c.application_id == self.application_id,
            )).mappings().first()
        file_format = next(kind for kind in ("docx", "pdf", "txt") if kind in document["files"])
        target = self.root / self.application_id / document["files"][file_format]
        original = target.read_bytes()
        original_open = Path.open
        changed = False

        def grow_on_open(path, *args, **kwargs):
            nonlocal changed
            if path == target and not changed:
                changed = True
                with original_open(path, "wb") as stream:
                    stream.write(b"x" * (MAX_EXPORT_BYTES + 1))
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", grow_on_open):
            response = self.client.get("/profiles/fixture/exports/backup.json")
        self.assertTrue(changed)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": {"code": "DATA_EXPORT_FAILED"}})
        self.assertNotIn("content-disposition", response.headers)
        target.write_bytes(original)
        self.store.engine.dispose()
        response = self.client.get("/profiles/fixture/exports/backup.json")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["document_files"])


if __name__ == "__main__":
    unittest.main()
