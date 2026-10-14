import csv
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.data_export import (
    DataExportError,
    export_applications_csv,
    export_jobs_csv,
    export_profile_backup,
    export_profile_json,
)


class DataExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-export-test-")
        database = Path(self.temporary.name) / "export.db"
        self.output_dir = Path(self.temporary.name) / "output"
        self.output_dir.mkdir()
        self.store = Store(f"sqlite:///{database.as_posix()}")
        self.store.migrate()
        self.store.save_profile({
            "id": "profile-a", "name": "Synthetic A", "email": "a@example.test",
            "facts": [], "answers": [],
        })
        self.store.save_profile({
            "id": "profile-b", "name": "Synthetic B", "email": "b@example.test",
            "facts": [], "answers": [],
        })
        self.store.save_profile({
            "id": "profile-empty", "name": "Synthetic Empty", "email": "empty@example.test",
            "facts": [], "answers": [],
        })
        with self.store.engine.begin() as connection:
            for suffix in ("a", "b"):
                upsert(connection, m.companies, {
                    "id": f"company-{suffix}", "name": f"Company {suffix.upper()}",
                    "domain": f"company-{suffix}.example.test",
                })
                upsert(connection, m.jobs, {
                    "id": f"job-{suffix}", "company_id": f"company-{suffix}",
                    "requisition": f"REQ-{suffix.upper()}", "title": f"Engineer {suffix.upper()}",
                    "description": f"Synthetic description {suffix.upper()}",
                    "skills": ["Python"], "location": "Remote", "remote_type": "REMOTE",
                    "attributes": {"safe": "metadata"},
                })
                upsert(connection, m.routes, {
                    "id": f"route-{suffix}", "job_id": f"job-{suffix}",
                    "company_id": f"company-{suffix}", "provider": "lever",
                    "tenant": f"tenant-{suffix}", "requisition": f"REQ-{suffix.upper()}",
                    "url": f"https://jobs.lever.co/tenant-{suffix}/REQ-{suffix.upper()}?token=PRIVATE_ROUTE_TOKEN",
                })
                upsert(connection, m.matches, {
                    "id": f"match-{suffix}", "job_id": f"job-{suffix}",
                    "profile_id": f"profile-{suffix}", "profile_version": "v1",
                    "score": 88.0, "missing": [], "components": {"skills": 80},
                })
                upsert(connection, m.applications, {
                    "id": f"application-{suffix}", "job_id": f"job-{suffix}",
                    "route_id": f"route-{suffix}", "profile_id": f"profile-{suffix}",
                    "profile_version": "v1", "state": "DRAFT", "target": "REVIEW",
                    "fact_ids": [], "trace": ["PRIVATE_TRACE_MARKER"],
                    "document_version": 1, "confirmation": {"raw": "PRIVATE_CONFIRMATION_MARKER"},
                    "created_at": f"2026-10-11T10:00:0{suffix == 'b'}Z", "submitted_at": None,
                })

            upsert(connection, m.provider_credentials, {
                "id": "credential-a", "profile_id": "profile-a", "provider": "openai",
                "encrypted_secret": b"PRIVATE_CREDENTIAL_MARKER", "created_at": "now",
                "updated_at": "now", "last_test_status": "NOT_TESTED", "rotation_count": 0,
            })
            upsert(connection, m.attempts, {
                "id": "attempt-a", "application_id": "application-a", "state": "FAILED",
                "steps": ["PRIVATE_SESSION_DIAGNOSTIC_MARKER"], "started_at": "2026-10-11T10:01:00Z",
            })
            upsert(connection, m.documents, {
                "id": "document-a", "application_id": "application-a", "kind": "resume",
                "version": 1, "profile_version": "v1",
                "files": {"txt": "v1/1/resume.txt"}, "fact_ids": [],
                "sha256": {"txt": hashlib.sha256(b"generated resume bytes").hexdigest()},
                "template": "plain", "created_at": "2026-10-11T10:02:00Z",
                "generation_model": None, "generation_context": {"raw": "PRIVATE_CONTEXT_MARKER"},
            })
            upsert(connection, m.postings, {
                "id": "posting-a", "job_id": "job-a", "source": "synthetic",
                "source_job_id": "posting-a", "source_url": "https://example.test/job",
                "application_url": "https://jobs.lever.co/tenant-a/REQ-A",
                "raw": {"diagnostic": "PRIVATE_RAW_DIAGNOSTIC_MARKER"},
                "fetched_at": "2026-10-11T10:03:00Z",
            })
            upsert(connection, m.uploaded_resumes, {
                "id": "upload-a", "profile_id": "profile-a", "filename": "source.pdf",
                "format": "pdf", "media_type": "application/pdf", "size_bytes": 18,
                "sha256": hashlib.sha256(b"uploaded pdf bytes").hexdigest(),
                "status": "READY", "created_at": "2026-10-11T10:04:00Z",
            })
            upsert(connection, m.resume_revisions, {
                "id": "resume-revision-a", "profile_id": "profile-a", "upload_id": "upload-a",
                "series_id": "series-a", "label": "Base", "version": 1,
                "created_at": "2026-10-11T10:04:00Z", "document_kind": "BASE_RESUME",
            })
        generated = self.output_dir / "application-a" / "v1" / "1" / "resume.txt"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"generated resume bytes")
        uploads = self.output_dir / "uploads"
        uploads.mkdir()
        (uploads / "upload-a.pdf").write_bytes(b"uploaded pdf bytes")

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def test_csv_exports_are_deterministic_profile_scoped_and_header_stable(self):
        jobs = export_jobs_csv(self.store, "profile-a")
        applications = export_applications_csv(self.store, "profile-a")

        job_rows = list(csv.DictReader(jobs.splitlines()))
        application_rows = list(csv.DictReader(applications.splitlines()))
        self.assertEqual(list(job_rows[0]), [
            "id", "company", "requisition", "title", "description", "skills", "location",
            "remote_type", "application_url", "match_score", "application_state",
        ])
        self.assertEqual([row["id"] for row in job_rows], ["job-a"])
        self.assertEqual([row["id"] for row in application_rows], ["application-a"])
        self.assertNotIn("PRIVATE_ROUTE_TOKEN", jobs + applications)
        self.assertNotIn("profile-b", jobs + applications)
        self.assertEqual(jobs, export_jobs_csv(self.store, "profile-a"))

    def test_json_export_and_backup_contain_profile_data_but_no_secrets_or_diagnostics(self):
        exported = export_profile_json(self.store, "profile-a")
        backup = export_profile_backup(self.store, "profile-a")
        encoded_backup = json.dumps(backup, ensure_ascii=False, sort_keys=True)

        self.assertEqual(json.loads(exported)["profile_id"], "profile-a")
        self.assertEqual(backup["profile_id"], "profile-a")
        self.assertEqual(backup["profile"]["name"], "Synthetic A")
        self.assertEqual([job["id"] for job in backup["jobs"]], ["job-a"])
        self.assertEqual([application["id"] for application in backup["applications"]],
                         ["application-a"])
        for marker in (
            "PRIVATE_CREDENTIAL_MARKER", "PRIVATE_ROUTE_TOKEN", "PRIVATE_TRACE_MARKER",
            "PRIVATE_CONFIRMATION_MARKER", "PRIVATE_SESSION_DIAGNOSTIC_MARKER",
            "PRIVATE_DOCUMENT_PATH_MARKER", "PRIVATE_CONTEXT_MARKER",
            "PRIVATE_RAW_DIAGNOSTIC_MARKER", "profile-b", "Company B",
        ):
            self.assertNotIn(marker, encoded_backup)
            self.assertNotIn(marker, exported)

    def test_empty_profile_has_explicit_empty_collections_and_csv_headers(self):
        jobs = export_jobs_csv(self.store, "profile-empty")
        applications = export_applications_csv(self.store, "profile-empty")
        backup = export_profile_backup(self.store, "profile-empty")

        self.assertEqual(len(list(csv.reader(jobs.splitlines()))), 1)
        self.assertEqual(len(list(csv.reader(applications.splitlines()))), 1)
        self.assertEqual(backup["jobs"], [])
        self.assertEqual(backup["applications"], [])
        self.assertIn("profile_versions", backup)
        self.assertEqual(backup["career_stories"], [])
        self.assertEqual(backup["uploaded_resumes"], [])

    def test_nested_authentication_fields_are_omitted_without_losing_answers(self):
        keys = ("api_key", "X-API-Key", "accessKey", "private-key",
                "Authorization", "Proxy_Authorization", "auth", "auth_header", "bearer",
                "authentication", "authentication_header", "authorization_header", "http_authorization")
        nested = {key: f"SYNTHETIC_AUTH_{index}" for index, key in enumerate(keys)}
        nested.update(work_authorization="Authorized to work", answer="Synthetic answer")
        with self.store.engine.begin() as connection:
            connection.execute(m.profile_versions.update().where(
                m.profile_versions.c.profile_id == "profile-a",
            ).values(answers=[{"metadata": nested}]))
        for exported in (export_profile_json(self.store, "profile-a"),
                         json.dumps(export_profile_backup(self.store, "profile-a"))):
            self.assertNotIn("SYNTHETIC_AUTH_", exported)
            self.assertIn("Authorized to work", exported)
            self.assertIn("Synthetic answer", exported)
        # Export redaction must never mutate the durable answers.
        with self.store.engine.connect() as connection:
            persisted = connection.execute(m.profile_versions.select().where(
                m.profile_versions.c.profile_id == "profile-a",
            )).mappings().first()["answers"]
        self.assertEqual(persisted, [{"metadata": nested}])

    def test_backup_includes_verified_profile_document_bytes_without_local_paths(self):
        backup = export_profile_backup(self.store, "profile-a", output_dir=self.output_dir)

        files = {item["id"]: item for item in backup["document_files"]}
        self.assertEqual(base64.b64decode(files["document-a"]["content_base64"]),
                         b"generated resume bytes")
        self.assertEqual(base64.b64decode(files["upload-a"]["content_base64"]),
                         b"uploaded pdf bytes")
        encoded = json.dumps(backup, sort_keys=True)
        self.assertNotIn(str(self.output_dir), encoded)
        self.assertNotIn("v1/1/resume.txt", encoded)

    def test_backup_rejects_document_path_escape_and_checksum_mismatch(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.documents.update().where(m.documents.c.id == "document-a").values(
                files={"txt": "../../outside.txt"},
            ))
        with self.assertRaisesRegex(DataExportError, "EXPORT_FILE_UNAVAILABLE"):
            export_profile_backup(self.store, "profile-a", output_dir=self.output_dir)

        with self.store.engine.begin() as connection:
            connection.execute(m.documents.update().where(m.documents.c.id == "document-a").values(
                files={"txt": "v1/1/resume.txt"},
            ))
        (self.output_dir / "application-a" / "v1" / "1" / "resume.txt").write_bytes(b"tampered")
        with self.assertRaisesRegex(DataExportError, "EXPORT_FILE_UNAVAILABLE"):
            export_profile_backup(self.store, "profile-a", output_dir=self.output_dir)

    def test_unknown_profile_and_oversized_output_fail_with_fixed_codes(self):
        with self.assertRaisesRegex(DataExportError, "PROFILE_NOT_FOUND"):
            export_profile_backup(self.store, "missing")

        with patch("services.data_export.MAX_EXPORT_BYTES", 32):
            with self.assertRaisesRegex(DataExportError, "EXPORT_TOO_LARGE"):
                export_profile_json(self.store, "profile-a")


if __name__ == "__main__":
    unittest.main()
