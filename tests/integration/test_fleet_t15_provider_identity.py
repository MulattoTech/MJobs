"""Team 15 connected checks for employer-bound ATS route identity."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert


class FleetT15ProviderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-t15-provider-identity-")
        self.root = Path(self.temp.name)
        self.database = self.root / "identity.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "canonical-employer", "name": "Canonical Employer",
                "domain": "canonical.example",
            })
            upsert(connection, m.companies, {
                "id": "evidence-employer", "name": "Different Employer",
                "domain": "different.example",
            })
            upsert(connection, m.jobs, {
                "id": "canonical-job", "company_id": "canonical-employer",
                "requisition": "canonical-req", "title": "Engineer",
                "description": "Synthetic fixture", "skills": [],
                "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "misbound-route", "job_id": "canonical-job",
                "company_id": "evidence-employer", "provider": "greenhouse",
                "tenant": "different-tenant", "requisition": "different-req",
                "url": "https://boards.greenhouse.io/different-tenant/jobs/42",
            })
            upsert(connection, m.company_ats_tenants, {
                "id": "different-employer-ats", "company_id": "evidence-employer",
                "provider": "greenhouse", "tenant_identifier": "different-tenant",
                "organization_identifier": "", "board_identifier": "",
                "confidence": 0.99, "evidence": ["synthetic verified board fixture"],
                "first_seen_at": "2026-10-16T00:00:00Z",
                "last_seen_at": "2026-10-16T00:00:00Z",
            })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_foreign_employer_ats_evidence_cannot_elevate_persisted_job_route(self):
        # Reopen the durable store to model service interruption after the route
        # and the other employer's ATS evidence have committed.
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        ))
        with client:
            response = client.get("/jobs/canonical-job/routes")
        self.assertEqual(response.status_code, 200, response.text)
        route = next(
            alias
            for option in response.json()["all_options"]
            for alias in option["aliases"]
            if alias["id"] == "misbound-route"
        )
        with self.subTest("foreign employer evidence cannot verify the route"):
            self.assertNotEqual(route["authority"], "VERIFIED_ATS")
        with self.subTest("foreign employer evidence cannot establish route capability"):
            self.assertNotEqual(route["adapter_knowledge"]["basis"], "ROUTE_AND_DISCOVERY")


if __name__ == "__main__":
    unittest.main()
