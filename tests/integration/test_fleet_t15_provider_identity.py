"""Team 15 connected checks for employer-bound ATS route identity."""
import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE


class FleetT15ProviderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-t15-provider-identity-")
        self.root = Path(self.temp.name)
        self.database = self.root / "identity.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.profile = copy.deepcopy(PROFILE)
        self.foreign_profile = copy.deepcopy(PROFILE)
        self.foreign_profile["id"] = "other-profile"
        self.store.save_profile(self.profile)
        self.store.save_profile(self.foreign_profile)
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
            self.assertEqual(route["authority"], "UNKNOWN")
        with self.subTest("foreign employer evidence cannot establish route capability"):
            self.assertEqual(route["adapter_knowledge"]["basis"], "UNKNOWN")
            self.assertIsNone(route["adapter_knowledge"]["provider"])
            self.assertIsNone(route["adapter_knowledge"]["ats_tenant_id"])
            self.assertEqual(route["adapter_knowledge"]["candidates"], [])

        for profile in (self.profile, self.foreign_profile):
            with self.subTest(profile=profile["id"]):
                scoped = client.get(
                    f"/jobs/canonical-job/routes?profile_id={profile['id']}"
                )
                self.assertEqual(scoped.status_code, 200, scoped.text)
                scoped_route = next(
                    alias
                    for option in scoped.json()["all_options"]
                    for alias in option["aliases"]
                    if alias["id"] == "misbound-route"
                )
                self.assertFalse(scoped_route["preferred_by_profile"])
                rejected = client.put(
                    f"/profiles/{profile['id']}/companies/canonical-employer/"
                    "preferred-application-route",
                    json={
                        "route_id": "misbound-route",
                        "preferred": True,
                        "request_id": f"foreign-route-{profile['id']}",
                    },
                )
                self.assertEqual(rejected.status_code, 404, rejected.text)

        with self.store.engine.connect() as connection:
            writes = connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.action == "APPLICATION_ROUTE_PREFERENCE",
            )).scalar_one()
        self.assertEqual(writes, 0)


if __name__ == "__main__":
    unittest.main()
