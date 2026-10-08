"""The local browser extraction preview is mounted and read-only in create_app."""
from copy import deepcopy
from http.client import HTTPConnection
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE


_FIELD_NAMES = (
    "title", "company", "description", "location", "employment_type",
    "date_posted", "valid_through", "source_job_id", "listing_url",
    "application_url",
)


class BrowserJobExtractionApplicationApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-browser-extraction-app-api-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.test"})
        self._seed_job("fixture-job", "fixture")
        self._seed_job("other-job", "other")
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        ))
        self.addCleanup(self.client.close)

    def _seed_job(self, job_id, profile_id):
        company_id = f"company-{job_id}"
        profile_version = self.store.profile(profile_id)["version"]
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company_id, "name": f"Synthetic {job_id}", "domain": "",
            })
            upsert(connection, m.jobs, {
                "id": job_id, "company_id": company_id, "requisition": job_id,
                "title": "Stored title", "description": "Stored description", "skills": [],
                "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.matches, {
                "id": f"match-{job_id}", "job_id": job_id, "profile_id": profile_id,
                "profile_version": profile_version, "score": 0.0,
                "missing": [], "components": {},
            })

    @staticmethod
    def _capture():
        return {"capture": {
            "raw_capture_ref": "local-capture-application-api-1",
            "provider": "greenhouse",
            "provider_fields": {
                "id": "fixture-job-41",
                "title": "Systems Administrator",
                "company_name": "Synthetic Employer",
                "content": "Maintain Linux infrastructure and backups.",
                "location": {"name": "Remote, US"},
                "absolute_url": "https://jobs.fixture.invalid/roles/41",
                "application_url": "https://jobs.fixture.invalid/roles/41#apply",
            },
            "selected_text": "Title: Conflicting selected title",
            "visible_text": "Title: Conflicting visible title",
        }}

    @staticmethod
    def _url(profile_id="fixture", job_id="fixture-job"):
        return f"/profiles/{profile_id}/jobs/{job_id}/browser-extraction-preview"

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            tables = (
                m.profiles, m.companies, m.jobs, m.routes, m.postings,
                m.posting_snapshots, m.matches, m.audit, m.applications,
                m.application_route_choices, m.documents, m.operations,
                m.ai_evaluation_runs, m.source_candidates,
                m.source_candidate_observations, m.source_candidate_reviews,
            )
            return tuple(
                [dict(row) for row in connection.execute(
                    select(table).order_by(*table.primary_key.columns)).mappings()]
                for table in tables
            )

    def test_create_app_route_is_profile_job_scoped_and_returns_provenance_and_unknowns(self):
        before = self._snapshot()
        capture = self._capture()
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), \
                patch.object(HTTPConnection, "connect", side_effect=AssertionError("network forbidden")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            response = self.client.post(self._url(), json=capture)
            foreign_job = self.client.post(self._url("fixture", "other-job"), json=capture)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(set(result), {
            "profile_id", "job_id", "raw_capture_ref", "fields",
            "unknown_fields", "review_required",
        })
        self.assertEqual(result["profile_id"], "fixture")
        self.assertEqual(result["job_id"], "fixture-job")
        self.assertEqual(result["raw_capture_ref"], "local-capture-application-api-1")
        self.assertEqual(tuple(result["fields"]), _FIELD_NAMES)
        self.assertEqual(result["fields"]["title"], {
            "value": "Systems Administrator", "source": "PROVIDER_FIELDS",
            "confidence": 0.9, "review_required": False,
        })
        self.assertEqual(result["fields"]["company"]["source"], "PROVIDER_FIELDS")
        self.assertEqual(result["fields"]["source_job_id"]["value"], "fixture-job-41")
        self.assertEqual(result["fields"]["listing_url"]["confidence"], 0.9)
        self.assertEqual(result["fields"]["application_url"]["source"], "PROVIDER_FIELDS")
        self.assertEqual(result["fields"]["location"], {
            "value": "Remote, US", "source": "PROVIDER_FIELDS",
            "confidence": 0.9, "review_required": False,
        })
        self.assertEqual(result["fields"]["date_posted"], {
            "value": None, "source": "UNKNOWN", "confidence": 0.0,
            "review_required": False,
        })
        self.assertEqual(result["unknown_fields"], [
            "employment_type", "date_posted", "valid_through",
        ])
        self.assertEqual(result["review_required"], [])
        self.assertEqual(foreign_job.status_code, 404, foreign_job.text)
        self.assertEqual(
            foreign_job.json()["detail"]["code"],
            "BROWSER_JOB_EXTRACTION_JOB_NOT_FOUND",
        )
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.store.count("applications"), 0)

    def test_malformed_json_is_rejected_before_extraction_or_storage_changes(self):
        before = self._snapshot()
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), \
                patch.object(HTTPConnection, "connect", side_effect=AssertionError("network forbidden")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
                patch("services.applications.job_extraction.extract_job_capture") as extract:
            malformed_json = self.client.post(
                self._url(), content=b'{"capture":',
                headers={"content-type": "application/json"},
            )
            malformed_shape = self.client.post(self._url(), json={"capture": {}})

        self.assertEqual(malformed_json.status_code, 422, malformed_json.text)
        self.assertEqual(malformed_shape.status_code, 422, malformed_shape.text)
        extract.assert_not_called()
        self.assertEqual(self._snapshot(), before)

    def test_transient_extraction_error_can_be_retried_without_persistence(self):
        before = self._snapshot()
        from services.applications.job_extraction import extract_job_capture as real_extract

        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), \
                patch.object(HTTPConnection, "connect", side_effect=AssertionError("network forbidden")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
                patch("services.applications.job_extraction.extract_job_capture",
                      side_effect=[ValueError("transient local extraction failure"), real_extract]) as extract:
            failed = self.client.post(self._url(), json=self._capture())
            recovered = self.client.post(self._url(), json=self._capture())

        self.assertEqual(failed.status_code, 422, failed.text)
        self.assertEqual(failed.json()["detail"]["code"], "BROWSER_JOB_EXTRACTION_CAPTURE_INVALID")
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["fields"]["title"]["source"], "PROVIDER_FIELDS")
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.store.count("applications"), 0)


if __name__ == "__main__":
    unittest.main()
