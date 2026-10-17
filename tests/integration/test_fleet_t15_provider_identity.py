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

    def test_same_company_route_and_ats_identity_survive_api_readback(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.routes, {
                "id": "same-company-route", "job_id": "canonical-job",
                "company_id": "canonical-employer", "provider": "workday",
                "tenant": "canonical-board", "requisition": "canonical-req",
                "url": "https://canonical-board.myworkdayjobs.com/External/job/42",
            })
            upsert(connection, m.company_ats_tenants, {
                "id": "canonical-employer-ats", "company_id": "canonical-employer",
                "provider": "workday", "tenant_identifier": "canonical-board",
                "organization_identifier": "", "board_identifier": "",
                "confidence": 0.97, "evidence": ["synthetic canonical employer board"],
                "first_seen_at": "2026-10-16T00:00:00Z",
                "last_seen_at": "2026-10-16T00:00:00Z",
            })
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
            if alias["id"] == "same-company-route"
        )
        self.assertEqual(route["authority"], "VERIFIED_ATS")
        self.assertEqual(route["adapter_knowledge"]["basis"], "ROUTE_AND_DISCOVERY")
        self.assertEqual(route["adapter_knowledge"]["ats_tenant_id"], "canonical-employer-ats")
        self.assertIsNone(route["adapter_knowledge"]["adapter_key"])
        self.assertFalse(route["adapter_knowledge"]["external_submission_enabled"])

    def test_foreign_verified_employer_domain_does_not_verify_same_company_route(self):
        with self.store.engine.begin() as connection:
            for domain_id, company_id, domain in (
                ("canonical-domain", "canonical-employer", "canonical.example"),
                ("foreign-domain", "evidence-employer", "different.example"),
            ):
                upsert(connection, m.company_domains, {
                    "id": domain_id,
                    "company_id": company_id, "domain": domain, "confidence": 1.0,
                    "verified": True, "verification_method": "synthetic-test",
                    "first_seen_at": "2026-10-16T00:00:00Z",
                    "last_verified_at": "2026-10-16T00:00:00Z",
                    "evidence": ["synthetic verified domain"],
                })
            upsert(connection, m.routes, {
                "id": "foreign-domain-route", "job_id": "canonical-job",
                "company_id": "canonical-employer", "provider": "employer",
                "tenant": "", "requisition": "canonical-req",
                "url": "https://different.example/jobs/42",
            })
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
            if alias["id"] == "foreign-domain-route"
        )
        self.assertNotEqual(route["authority"], "VERIFIED_EMPLOYER")

    def test_personal_workspace_does_not_grant_fixture_adapter_for_matching_route(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.routes, {
                "id": "personal-mock-route", "job_id": "canonical-job",
                "company_id": "canonical-employer", "provider": "mock-ats",
                "tenant": "canonical-fixture", "requisition": "canonical-req",
                "url": "https://fixtures.example/jobs/42",
            })
            upsert(connection, m.company_ats_tenants, {
                "id": "canonical-mock-ats", "company_id": "canonical-employer",
                "provider": "mock-ats", "tenant_identifier": "canonical-fixture",
                "organization_identifier": "", "board_identifier": "",
                "confidence": 0.97, "evidence": ["synthetic fixture identity"],
                "first_seen_at": "2026-10-16T00:00:00Z",
                "last_seen_at": "2026-10-16T00:00:00Z",
            })
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
            if alias["id"] == "personal-mock-route"
        )
        self.assertEqual(route["adapter_knowledge"]["basis"], "ROUTE_AND_DISCOVERY")
        self.assertEqual(route["adapter_knowledge"]["provider"], "mock-ats")
        self.assertIsNone(route["adapter_knowledge"]["adapter_key"])
        self.assertNotEqual(route["adapter_knowledge"]["adapter_status"], "FIXTURE_ONLY")
        self.assertFalse(route["adapter_knowledge"]["external_submission_enabled"])


if __name__ == "__main__":
    unittest.main()
