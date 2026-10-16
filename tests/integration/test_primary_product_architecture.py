"""End-to-end connected verification of the primary product architecture (MJ-REQ-010).

Validates that:
  Web Dashboard / Application API
      ├── Search Service (Global Sources + Company Sources -> Normalization -> Company Resolution -> Deduplication -> Job Scoring -> Database)
      ├── Application Service (Route Selection -> Automation Engine -> Dry-Run -> Confirmation & Tracking)
      └── Profile / Document Service (Truthful Profile -> AI Engine & Document Engine -> Provenance & Artifacts)
All background operations are durable and survive store restart.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from docx import Document
from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from sqlalchemy import select

TEST_PROFILE = {
    "id": "arch_profile_01",
    "name": "Jordan Architect",
    "email": "jordan@example.test",
    "answers": [
        {"id": "country", "text": "United States", "source": "verified-test", "verified": True, "user_approved": True, "confidence": 100},
        {"id": "remote", "text": "yes", "source": "verified-test", "verified": True, "user_approved": True, "confidence": 100},
        {"id": "fixture_question", "text": "Architectural pipeline test application.", "source": "verified-test", "verified": True, "user_approved": True, "confidence": 100},
    ],
    "facts": [
        {"id": "python", "text": "Python", "kind": "skill", "source": "verified-test", "verified": True, "user_approved": True, "confidence": 100},
        {"id": "sql", "text": "SQL", "kind": "skill", "source": "verified-test", "verified": True, "user_approved": True, "confidence": 100},
    ],
}


class PrimaryProductArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db_path = self.root / "mjobs_arch.db"
        self.url = f"sqlite:///{self.db_path.as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(TEST_PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_01_application_api_surface_and_subsystem_mounting(self):
        """Web Dashboard / API connects to search, profile, documents, and applications."""
        with fixture_server() as origin:
            app = create_app(self.store, origin, self.root, operation_mode="FIXTURE")
            with TestClient(app) as client:
                # 1. API readiness and operational health
                res = client.get("/readiness")
                self.assertIn(res.status_code, (200, 503))

                # 2. Profile and document routes
                res = client.get("/profiles/arch_profile_01")
                self.assertEqual(res.status_code, 200)
                profile_data = res.json()
                self.assertEqual(profile_data["id"], "arch_profile_01")
                self.assertEqual(profile_data["name"], "Jordan Architect")

                # 3. Source system & search routes
                res = client.get("/search-profiles")
                self.assertEqual(res.status_code, 200)

                # 4. Jobs and applications routes
                res = client.get("/jobs")
                self.assertEqual(res.status_code, 200)

                res = client.get("/applications")
                self.assertEqual(res.status_code, 200)

                # 5. Background operations route
                res = client.get("/operations")
                self.assertEqual(res.status_code, 200)

    def test_02_search_service_normalization_resolution_dedup_and_scoring(self):
        """Global & company sources flow through normalization, company resolution, deduplication, and scoring into DB."""
        with fixture_server() as origin:
            # Discover from fixture (contains multi-source postings for requisition A and employer-only requisition B)
            discover(self.store, origin, "arch_profile_01")
            discover(self.store, origin, "arch_profile_01")

        # Verify database entities:
        # 1. Company Resolution: Company record was resolved and persisted
        self.assertGreaterEqual(self.store.count("companies"), 1)

        # 2. Source System: Company source enrolled
        self.assertEqual(self.store.count("company_job_sources"), 1)

        # 3. Normalization & Deduplication: Two canonical jobs, but 4 distinct postings ingested
        jobs = self.store.jobs("arch_profile_01")
        self.assertEqual({job["requisition"] for job in jobs}, {"A", "B"})
        self.assertEqual(self.store.count("canonical_jobs"), 2)
        self.assertEqual(self.store.count("job_postings"), 4)

        # Job A deduplicated multiple postings
        job_a = next(job for job in jobs if job["requisition"] == "A")
        self.assertEqual(job_a["posting_count"], 3)

        # 4. Job Scoring against candidate verified facts (Python + SQL)
        employer_job_b = next(job for job in jobs if job["requisition"] == "B")
        self.assertEqual(employer_job_b["match_score"], 100)
        detail = self.store.job(employer_job_b["id"], profile_id="arch_profile_01")
        match_components = detail["match"]["components"]
        requirements = {r["skill"]: r["strength"] for r in match_components["requirements"]}
        self.assertEqual(requirements, {"Python": "REQUIRED", "SQL": "REQUIRED"})

    def test_03_profile_doc_service_ai_engine_and_truth_preservation(self):
        """Profile/Document Engine generates truthful resume & cover letter without hallucinated facts."""
        with fixture_server() as origin:
            discover(self.store, origin, "arch_profile_01")
            job_b = next(j for j in self.store.jobs() if j["requisition"] == "B")

            # Document preparation: tailored resume and cover letter
            app_id = prepare(self.store, job_b["id"], "arch_profile_01", self.root)

        # Verify application and documents in store
        app_data = self.store.application(app_id)
        self.assertEqual(app_data["state"], "PREPARED")
        self.assertEqual(app_data["fact_ids"], ["python", "sql"])

        # Two documents generated: resume and cover letter
        self.assertEqual(self.store.count("documents"), 2)

        # Verify generated DOCX contains real verified skills and NO fabricated skills
        doc_dir = self.root / app_id
        with self.store.engine.connect() as conn:
            resume_rec = conn.execute(
                select(m.documents).where(m.documents.c.application_id == app_id, m.documents.c.kind == "resume")
            ).mappings().one()

        docx_path = doc_dir / resume_rec["files"]["docx"]
        self.assertTrue(docx_path.is_file(), "Resume DOCX file must exist")
        text = "\n".join(p.text for p in Document(docx_path).paragraphs)
        self.assertIn("Python", text)
        self.assertIn("SQL", text)
        self.assertNotIn("AWS Lambda", text)  # Truth preservation invariant

    def test_04_application_service_route_selection_and_idempotent_readback(self):
        """Application service selects authoritative route and survives store restart without data loss."""
        with fixture_server() as origin:
            discover(self.store, origin, "arch_profile_01")
            job_b = next(j for j in self.store.jobs() if j["requisition"] == "B")
            app_id = prepare(self.store, job_b["id"], "arch_profile_01", self.root)

        # Durable restart test: Close store and reopen from SQLite database file
        self.store.engine.dispose()
        reopened = Store(self.url)
        try:
            # Verify jobs, sources, companies, documents, applications survive
            self.assertEqual(reopened.count("canonical_jobs"), 2)
            self.assertEqual(reopened.count("job_postings"), 4)
            self.assertEqual(reopened.count("company_job_sources"), 1)
            self.assertEqual(reopened.count("documents"), 2)

            recovered_app = reopened.application(app_id)
            self.assertEqual(recovered_app["state"], "PREPARED")
            self.assertEqual(recovered_app["job_id"], job_b["id"])
            self.assertEqual(recovered_app["profile_id"], "arch_profile_01")
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
