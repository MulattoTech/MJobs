"""Profile/job-scoped, read-only cover-letter preview API contract."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.cover_letter_preview import cover_letter_preview_fingerprint
from tests.integration.test_spine import PROFILE


class CoverLetterPreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-cover-letter-preview-api-")
        self.root = Path(self.temporary.name)
        self.store = Store(f"sqlite:///{(self.root / 'preview.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(copy.deepcopy(PROFILE))
        # Discover only from the repository's synthetic local ATS, then close it.
        # Preview requests must work from saved snapshots and make no network calls.
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
        self.profile = self.store.profile("fixture")
        self.app = FastAPI()
        from apps.api.cover_letter_preview import create_cover_letter_preview_router
        self.app.include_router(create_cover_letter_preview_router(self.store))
        self.client = TestClient(self.app)

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def _path(self, profile_id="fixture", job_id=None):
        return f"/profiles/{profile_id}/jobs/{job_id or self.job['id']}/cover-letter-preview"

    def _request(self, **overrides):
        value = {
            "expected_profile_version": self.profile["version"],
            "expected_job_fingerprint": cover_letter_preview_fingerprint(self.job),
            "style": "warm",
            "length": "concise",
        }
        value.update(overrides)
        return self._post_readonly(self._path(), value)

    def _post_readonly(self, path, body):
        writes = []

        def detect_write(_connection, _cursor, statement, _parameters, _context, _many):
            verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
                writes.append(verb)

        event.listen(self.store.engine, "before_cursor_execute", detect_write)
        try:
            response = self.client.post(path, json=body)
        finally:
            event.remove(self.store.engine, "before_cursor_execute", detect_write)
        self.assertEqual(writes, [], f"preview endpoint attempted database writes: {writes}")
        return response

    def _table_count(self, table):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(table)).scalar_one()

    def _write_counts(self):
        return tuple(self._table_count(table) for table in (m.applications, m.documents, m.audit))

    def test_preview_returns_scoped_snapshots_and_provenance_without_writes_or_network(self):
        before = self._write_counts()

        response = self._request()

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["profile_id"], "fixture")
        self.assertEqual(result["profile_version"], self.profile["version"])
        self.assertEqual(result["job_id"], self.job["id"])
        self.assertEqual(result["job_fingerprint"], cover_letter_preview_fingerprint(self.job))
        self.assertIn("Application for", result["preview_text"])
        self.assertEqual(result["style"], "warm")
        self.assertEqual(result["length"], "concise")
        self.assertTrue(result["source_facts"])
        for fact in result["source_facts"]:
            self.assertTrue(fact["fact_id"])
            self.assertTrue(fact["source"])
            self.assertTrue(fact["verified"])
            self.assertTrue(fact["user_approved"])
            self.assertRegex(fact["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(fact["matched_terms"])
        self.assertTrue(result["job_evidence"]["skills"])
        self.assertTrue(result["job_evidence"]["responsibilities"])
        self.assertTrue(result["job_evidence"]["skill_source"])
        self.assertTrue(result["job_evidence"]["responsibility_source"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["document_created"])
        self.assertIsNone(result["company_context"])
        self.assertFalse(result["company_context_evidence"]["included"])
        self.assertEqual(result["company_context_evidence"]["status"], "OMITTED_UNVERIFIED")
        self.assertIn("USER_PROVIDED / CONTENT UNVERIFIED", result["company_context_caveat"])
        self.assertEqual(self._write_counts(), before)

    def test_stale_profile_or_job_snapshot_is_rejected_with_stable_conflict(self):
        before = self._write_counts()
        stale_profile = self._request(expected_profile_version="a" * 64)
        stale_job = self._request(expected_job_fingerprint="b" * 64)

        self.assertEqual(stale_profile.status_code, 409)
        self.assertEqual(stale_profile.json()["detail"]["code"], "COVER_LETTER_PREVIEW_PROFILE_STALE")
        self.assertEqual(stale_job.status_code, 409)
        self.assertEqual(stale_job.json()["detail"]["code"], "COVER_LETTER_PREVIEW_JOB_STALE")
        self.assertEqual(self._write_counts(), before)

    def test_missing_profile_or_job_is_not_disclosed_as_a_preview(self):
        body = {
            "expected_profile_version": self.profile["version"],
            "expected_job_fingerprint": cover_letter_preview_fingerprint(self.job),
            "style": "professional", "length": "normal",
        }
        before = self._write_counts()
        missing_profile = self._post_readonly(self._path(profile_id="missing-profile"), body)
        missing_job = self._post_readonly(self._path(job_id="missing-job"), body)

        self.assertEqual(missing_profile.status_code, 404)
        self.assertEqual(missing_job.status_code, 404)
        self.assertEqual(self._write_counts(), before)

    def test_missing_approved_matching_facts_fail_without_creating_documents(self):
        profile = copy.deepcopy(PROFILE)
        profile["facts"] = [{
            "id": "pending-unrelated", "kind": "skill", "text": "Unrelated pending skill",
            "source": "synthetic fixture", "verified": False, "user_approved": False,
            "confidence": 20,
        }]
        self.store.save_profile(profile)
        self.profile = self.store.profile("fixture")
        before = self._write_counts()

        response = self._request()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "COVER_LETTER_RELEVANT_FACTS_MISSING")
        self.assertEqual(self._write_counts(), before)

    def test_reviewed_user_company_context_keeps_unverified_content_caveat(self):
        content = "Example Systems provides regional infrastructure support."
        context = {
            "id": "context-fixture", "company_id": self.job["company_id"],
            "company_source_id": "source-fixture", "version": 1,
            "content_origin": "USER_PROVIDED", "content_status": "UNVERIFIED",
            "content": content, "source_url": "https://careers.example.test/company",
            "source_host": "careers.example.test", "verified_domain": "example.test",
            "source_identity_method": "VERIFIED_EXISTING_DOMAIN", "identity_status": "VERIFIED",
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "user_reviewed": True, "user_approved": True,
            "reviewed_at": "2026-10-09T00:00:00Z",
        }
        attributes = dict(self.job.get("attributes") or {})
        attributes["approved_company_context"] = context
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.job["id"]).values(
                attributes=attributes,
            ))
        self.job["attributes"] = attributes

        response = self._request()

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["company_context"]["content_status"], "UNVERIFIED")
        self.assertEqual(result["company_context"]["content_origin"], "USER_PROVIDED")
        self.assertIn("USER_PROVIDED / CONTENT UNVERIFIED", result["company_context"]["caveat"])
        self.assertIn("USER_PROVIDED", result["preview_text"])
        self.assertIn("CONTENT UNVERIFIED", result["preview_text"])
        self.assertIn(content, result["preview_text"])

    def test_invalid_style_or_length_is_rejected_before_preview(self):
        for overrides in ({"style": "overjoyed"}, {"length": "unbounded"}):
            with self.subTest(overrides=overrides):
                response = self._request(**overrides)
                self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
