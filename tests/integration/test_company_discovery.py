"""Dedicated company-resolution, trigger, and flywheel acceptance."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.resolution import resolve_company
from services.company_discovery.triggers import queue_company_discovery
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class CompanyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_resolver_returns_identity_confidence_and_evidence(self):
        candidate = {
            "company_name": "Example Systems Inc.",
            "company_website": "https://example.test/company",
            "application_url": "https://careers.example.test/jobs/123",
        }
        first = resolve_company(self.store, candidate)
        self.assertTrue(first["is_new"])
        self.assertEqual(first["company_id"], identity(candidate["company_website"]))
        self.assertGreaterEqual(first["confidence"], 0.90)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": first["company_id"],
                "name": "Example Systems Inc.",
                "domain": candidate["company_website"],
            })
        second = resolve_company(self.store, candidate)
        self.assertFalse(second["is_new"])
        self.assertEqual(second["company_id"], first["company_id"])
        self.assertIn("OFFICIAL_DOMAIN_EXACT", second["evidence"])

    def test_trigger_is_durable_and_idempotent(self):
        candidate = {
            "company_name": "Example Systems Inc.",
            "company_website": "http://127.0.0.1:8090/company",
        }
        first = queue_company_discovery(
            self.store, candidate, "PREVIOUSLY_UNSEEN_COMPANY", "fixture"
        )
        second = queue_company_discovery(
            self.store, candidate, "PREVIOUSLY_UNSEEN_COMPANY", "fixture"
        )
        self.assertEqual(first["operation_id"], second["operation_id"])
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == first["operation_id"]
            )).mappings().one()
        self.assertEqual(row["kind"], "COMPANY_DISCOVER")
        self.assertEqual(row["params"]["trigger_reason"], "PREVIOUSLY_UNSEEN_COMPANY")
        self.assertEqual(
            row["params"]["candidate"]["trigger_evidence"],
            ["PREVIOUSLY_UNSEEN_COMPANY"],
        )

    def test_durable_company_operation_enrolls_source_and_immediately_imports_employer_only_job(self):
        with fixture_server() as origin:
            candidate = {
                "company_name": "Example Systems Inc.",
                "company_website": origin + "/company",
                "application_url": origin + "/board/apply/A",
            }
            queued = queue_company_discovery(
                self.store, candidate, "PREVIOUSLY_UNSEEN_COMPANY", "fixture"
            )
            environment = {
                "MJOBS_DATABASE_URL": self.url,
                "MJOBS_MOCK_ORIGIN": origin,
                "MJOBS_OUTPUT_DIR": str(self.root),
                "MJOBS_OPERATION_MODE": "FIXTURE",
            }
            with patch.dict(os.environ, environment, clear=False):
                result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "COMPLETED")
        jobs = self.store.jobs("fixture")
        self.assertEqual({row["requisition"] for row in jobs}, {"A", "B"})
        self.assertEqual(self.store.count("company_job_sources"), 1)
        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.company_sources)).mappings().one())
        self.assertTrue(source["discovered_automatically"])
        self.assertEqual(next(row for row in jobs if row["requisition"] == "B")["match_score"], 100)
    def test_api_can_queue_user_added_company_without_performing_network_io(self):
        with fixture_server() as origin, TestClient(create_app(
            self.store, origin, self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        )) as client:
            response = client.post("/companies/discovery", json={
                "company_name": "Example Systems Inc.",
                "company_website": origin + "/company",
                "reason": "USER_ADDED_COMPANY",
                "profile_id": "fixture",
            })
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "QUEUED")
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == body["operation_id"]
            )).mappings().one()
        self.assertEqual(operation["kind"], "COMPANY_DISCOVER")

    def test_already_monitored_company_is_not_requeued_for_ordinary_trigger(self):
        with fixture_server() as origin:
            candidate = {
                "company_name": "Example Systems Inc.",
                "company_website": origin + "/company",
            }
            company_id = identity(candidate["company_website"])
            with self.store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": company_id, "name": candidate["company_name"],
                    "domain": candidate["company_website"],
                })
                upsert(connection, m.company_sources, {
                    "id": "source", "company_id": company_id,
                    "provider": "mock-ats", "tenant": "example",
                    "endpoint": origin + "/ats/example/jobs",
                    "discovered_automatically": True,
                })
            result = queue_company_discovery(
                self.store, candidate, "HIGH_MATCH_JOB", "fixture"
            )
        self.assertEqual(result["status"], "ALREADY_MONITORED")
        self.assertIsNone(result["operation_id"])


if __name__ == "__main__":
    unittest.main()
