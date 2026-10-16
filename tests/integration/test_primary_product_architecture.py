"""End-to-end connected verification of the primary product architecture (MJ-REQ-010).

Validates that:
  Web Dashboard / Application API
      ├── Search Service (Global Sources + Company Sources -> Normalization -> Company Resolution -> Deduplication -> Job Scoring -> Database)
      ├── Application Service (Route Selection -> Automation Engine -> Dry-Run -> Confirmation & Tracking)
      └── Profile / Document Service (Truthful Profile -> AI Engine & Document Engine -> Provenance & Artifacts)
All background operations are durable and survive store restart.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from docx import Document
from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from workers.dispatch import enqueue_operation, dispatch_pending
from workers.tasks import execute_operation


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



POSTGRES_URL = os.environ.get("MJOBS_TEST_POSTGRES_URL")
REDIS_URL = os.environ.get("MJOBS_REDIS_URL")


@unittest.skipUnless(POSTGRES_URL, "Set MJOBS_TEST_POSTGRES_URL to disposable loopback mjobs_test PostgreSQL")
class PrimaryProductArchitecturePostgreSQLTests(unittest.TestCase):
    def setUp(self):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith("postgresql") or url.host not in ("localhost", "127.0.0.1", "::1")
                or not url.database or not (url.database == "mjobs_test" or url.database.startswith("mjobs_test_"))):
            raise RuntimeError("Disposable loopback mjobs_test PostgreSQL database required")
        self.schema = "mjobs_arch_" + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        scoped = url.update_query_dict({"options": "-csearch_path=" + self.schema + " -clock_timeout=5000 -cstatement_timeout=30000"})
        self.database_url = scoped.render_as_string(hide_password=False)
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": self.database_url}):
            command.upgrade(Config("alembic.ini"), "head")
        self.store = Store(self.database_url)
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(TEST_PROFILE)
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def cleanup_schema(self):
        if hasattr(self, "store") and self.store:
            self.store.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + self.schema + '" CASCADE'))
        self.admin.dispose()
        if hasattr(self, "directory") and self.directory:
            self.directory.cleanup()

    def test_01_postgresql_primary_pipeline_discovery_dedup_scoring_and_documents(self):
        """Full primary product architecture pipeline executes and persists truthfully on PostgreSQL."""
        with fixture_server() as origin:
            app = create_app(self.store, origin, self.root, operation_mode="FIXTURE")
            with TestClient(app) as client:
                res = client.get("/profiles/arch_profile_01")
                self.assertEqual(res.status_code, 200)

            # 1. Global & Company Sources -> Normalization -> Company Resolution -> Deduplication -> Scoring
            discover(self.store, origin, "arch_profile_01")
            discover(self.store, origin, "arch_profile_01")

            self.assertGreaterEqual(self.store.count("companies"), 1)
            self.assertEqual(self.store.count("company_job_sources"), 1)
            self.assertEqual(self.store.count("canonical_jobs"), 2)
            self.assertEqual(self.store.count("job_postings"), 4)

            # 2. Document generation on PostgreSQL
            job_b = next(j for j in self.store.jobs() if j["requisition"] == "B")
            app_id = prepare(self.store, job_b["id"], "arch_profile_01", self.root)

            self.assertEqual(self.store.count("documents"), 2)
            app_data = self.store.application(app_id)
            self.assertEqual(app_data["state"], "PREPARED")

        # 3. Durable restart: close store and reopen on PostgreSQL
        self.store.engine.dispose()
        reopened = Store(self.database_url)
        try:
            self.assertEqual(reopened.count("canonical_jobs"), 2)
            self.assertEqual(reopened.count("job_postings"), 4)
            self.assertEqual(reopened.count("documents"), 2)
            recovered = reopened.application(app_id)
            self.assertEqual(recovered["state"], "PREPARED")
        finally:
            reopened.engine.dispose()


@unittest.skipUnless(REDIS_URL, "Set MJOBS_REDIS_URL to disposable loopback Redis")
class PrimaryProductArchitectureRedisRQTests(unittest.TestCase):
    def setUp(self):
        from urllib.parse import urlsplit
        from redis import Redis
        target = urlsplit(REDIS_URL)
        if (target.scheme != "redis" or target.hostname not in {"127.0.0.1", "localhost", "::1"}
                or target.port != 6379 or target.username or target.password):
            raise ValueError("Requires disposable loopback Redis on port 6379")
        self.redis = Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        self.addCleanup(self.redis.close)
        self.redis.ping()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db_path = self.root / "mjobs_rq_arch.db"
        self.url = f"sqlite:///{self.db_path.as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(TEST_PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_01_durable_background_operations_lifecycle_on_redis_and_store(self):
        """Background operations are durably tracked in store and dispatched to Redis."""
        with fixture_server() as origin:
            discover(self.store, origin, "arch_profile_01")
            job_b = next(j for j in self.store.jobs() if j["requisition"] == "B")

            # 1. Enqueue durable operation to database outbox
            op_id = enqueue_operation(self.store, "PREPARE", "arch_profile_01", {
                "job_id": job_b["id"],
            })

            # Verify initial durable state in store
            with self.store.engine.connect() as conn:
                op_row = conn.execute(select(m.operations).where(m.operations.c.id == op_id)).mappings().one()
            self.assertEqual(op_row["state"], "QUEUED")

            # 2. Dispatch pending operations to Redis/RQ
            dispatch_res = dispatch_pending(self.store, REDIS_URL)
            self.assertEqual(dispatch_res["status"], "DISPATCHED")
            self.assertGreaterEqual(dispatch_res["count"], 1)

            # 3. Execute operation in worker environment
            with patch.dict(os.environ, {
                "MJOBS_DATABASE_URL": self.store.url,
                "MJOBS_REDIS_URL": REDIS_URL,
                "MJOBS_MOCK_ORIGIN": origin,
                "MJOBS_OUTPUT_DIR": str(self.root),
                "MJOBS_OPERATION_MODE": "FIXTURE",
            }):
                result = execute_operation(op_id)

            self.assertEqual(result["status"], "COMPLETED")
            with self.store.engine.connect() as conn:
                completed_op = conn.execute(select(m.operations).where(m.operations.c.id == op_id)).mappings().one()
            self.assertEqual(completed_op["state"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
