"""Discovery knowledge is internal routing metadata, never submission authority."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import register_ats_tenant
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        with self.store.engine.begin() as c:
            for company in ("company", "other"):
                upsert(c, m.companies, dict(id=company, name="Same Name", domain=""))
            upsert(c, m.jobs, dict(id="job", company_id="company", requisition="1",
                title="Systems Administrator", description="Manage systems", skills=[],
                location="Remote", remote_type="REMOTE"))
            upsert(c, m.routes, dict(id="route", job_id="job", company_id="company",
                provider="rss", tenant="company", requisition="1", url="https://example.test/jobs/1"))

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def tenant(self, provider="greenhouse", tenant="acme", company="company", evidence=None):
        return register_ats_tenant(self.store, company, provider=provider,
            tenant_identifier=tenant, confidence=0.97,
            evidence=["STORED_DISCOVERY_EVIDENCE"] if evidence is None else evidence)

    def knowledge(self):
        from services.applications.knowledge import application_adapter_knowledge
        with self.store.engine.connect() as c:
            return application_adapter_knowledge(c, "route", operation_mode="PERSONAL")

    def test_reuses_stored_company_identity_without_fetch_or_detection(self):
        tenant = self.tenant()
        with patch("httpx.Client.get", side_effect=AssertionError("No network")), \
                patch("services.ats_detection.detector.detect_ats", side_effect=AssertionError("No rediscovery")):
            result = self.knowledge()
        self.assertEqual(result["provider"], "greenhouse")
        self.assertEqual(result["basis"], "COMPANY_DISCOVERY")
        self.assertEqual(result["ats_tenant_id"], tenant["id"])
        self.assertEqual(result["adapter_status"], "UNSUPPORTED")
        self.assertIsNone(result["adapter_key"])
        self.assertFalse(result["external_submission_enabled"])

    def test_same_name_other_company_and_empty_evidence_do_not_supply_knowledge(self):
        self.tenant(company="other")
        self.tenant(evidence=[])
        result = self.knowledge()
        self.assertEqual(result["basis"], "UNKNOWN")
        self.assertIsNone(result["provider"])

    def test_multiple_tenants_require_review_instead_of_highest_confidence_guess(self):
        self.tenant()
        self.tenant(provider="lever", tenant="division")
        result = self.knowledge()
        self.assertEqual(result["basis"], "AMBIGUOUS")
        self.assertIsNone(result["provider"])
        self.assertEqual(len(result["candidates"]), 2)

    def test_weak_detection_is_review_metadata_not_established_ats(self):
        self.tenant()
        with self.store.engine.begin() as c:
            c.execute(update(m.company_ats_tenants).values(confidence=0.45))
        result = self.knowledge()
        self.assertEqual(result["basis"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["adapter_status"], "REVIEW_REQUIRED")
        self.assertIsNone(result["provider"])
        self.assertEqual(len(result["candidates"]), 1)

    def test_explicit_route_reuses_only_exact_provider_and_tenant(self):
        tenant = self.tenant()
        self.tenant(provider="lever", tenant="division")
        with self.store.engine.begin() as c:
            c.execute(update(m.routes).values(provider="greenhouse", tenant="acme"))
        result = self.knowledge()
        self.assertEqual(result["basis"], "ROUTE_AND_DISCOVERY")
        self.assertEqual(result["ats_tenant_id"], tenant["id"])
        with self.store.engine.begin() as c:
            c.execute(update(m.routes).values(tenant="different-board"))
        result = self.knowledge()
        self.assertEqual(result["basis"], "ROUTE")
        self.assertIsNone(result["ats_tenant_id"])

    def test_mock_knowledge_never_turns_a_real_route_into_a_fixture_adapter(self):
        self.tenant(provider="mock-ats")
        self.assertIsNone(self.knowledge()["adapter_key"])
        with self.store.engine.begin() as c:
            c.execute(update(m.routes).values(provider="mock-ats", tenant="acme"))
        self.assertIsNone(self.knowledge()["adapter_key"])
        from services.applications.knowledge import application_adapter_knowledge
        with self.store.engine.begin() as c:
            c.execute(update(m.workspace_identity).values(operation_mode="FIXTURE"))
        with self.store.engine.connect() as c:
            result = application_adapter_knowledge(c, "route", operation_mode="FIXTURE")
        self.assertEqual(result["adapter_key"], "mock-ats")
        self.assertFalse(result["external_submission_enabled"])

    def test_preparation_audit_and_api_expose_knowledge_without_changing_route(self):
        tenant = self.tenant()
        application_id = prepare(self.store, "job", "fixture", self.root)
        with self.store.engine.connect() as c:
            event = c.execute(select(m.audit).where(m.audit.c.action == "DOCUMENTS_PREPARED")).mappings().one()
        self.assertEqual(event["payload"]["adapter_knowledge"]["ats_tenant_id"], tenant["id"])
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root)) as client:
            application = client.get(f"/applications/{application_id}").json()
            routes = client.get("/jobs/job/routes").json()
            self.assertEqual(application["adapter_knowledge"]["provider"], "greenhouse")
            self.assertEqual(routes["all_options"][0]["preferred_route"]["adapter_knowledge"]["provider"], "greenhouse")
            self.assertEqual(client.post(f"/applications/{application_id}/mock-run", json={"submit": True}).status_code, 409)
        self.assertEqual(application["route_id"], "route")
        self.assertEqual(application["target"], "REAL")
        self.assertEqual(application["state"], "PREPARED")
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_fixture_worker_discovery_reaches_preparation_and_survives_reopen(self):
        from fixtures.mock_server import fixture_server
        from services.company_discovery.triggers import queue_company_discovery
        from workers.tasks import execute_operation
        # A separate synthetic database is bound to FIXTURE before any data exists.
        store = Store(f"sqlite:///{(self.root / 'fixture.db').as_posix()}")
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(PROFILE)
        try:
            with fixture_server() as origin:
                queued = queue_company_discovery(store, {
                    "company_name": "Example Systems Inc.",
                    "company_website": origin + "/company",
                    "application_url": origin + "/board/apply/A",
                }, "PREVIOUSLY_UNSEEN_COMPANY", "fixture")
                with patch.dict(os.environ, MJOBS_DATABASE_URL=store.url, MJOBS_MOCK_ORIGIN=origin,
                                MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="FIXTURE"):
                    self.assertEqual(execute_operation(queued["operation_id"])["status"], "COMPLETED")
                job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
                application_id = prepare(store, job["id"], "fixture", self.root)
            # The fixture server is gone: only persisted knowledge can answer now.
            store.engine.dispose()
            with TestClient(create_app(store, origin, self.root, operation_mode="FIXTURE")) as client:
                result = client.get(f"/applications/{application_id}").json()
                self.assertEqual(result["adapter_knowledge"]["basis"], "ROUTE_AND_DISCOVERY")
                self.assertEqual(result["adapter_knowledge"]["adapter_key"], "mock-ats")
                self.assertEqual(result["state"], "PREPARED")
                self.assertFalse(client.get("/settings").json()["external_submission_enabled"])
        finally:
            store.engine.dispose()
