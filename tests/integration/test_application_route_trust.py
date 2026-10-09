"""Profile-owned route preferences remain explicit, durable, and company-scoped."""
import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.dedupe.routes import plan_application_routes
from tests.integration.test_spine import PROFILE


class ApplicationRouteTrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'routes.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.profile = copy.deepcopy(PROFILE)
        self.store.save_profile(self.profile)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "company-a", "name": "Example Employer", "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": "job-a", "company_id": "company-a", "requisition": "req-a",
                "title": "Systems Engineer", "description": "Support systems.",
                "skills": [], "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.companies, {
                "id": "company-b", "name": "Other Employer", "domain": "other.test",
            })
            upsert(connection, m.jobs, {
                "id": "job-b", "company_id": "company-b", "requisition": "req-other",
                "title": "Other Engineer", "description": "Support systems.",
                "skills": [], "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            for route_id, provider, tenant, req in (
                ("board-a", "integrated-board-a", "alpha", "req-a"),
                ("board-b", "integrated-board-b", "beta", "req-b"),
                ("other-company", "integrated-board-c", "other", "req-other"),
            ):
                upsert(connection, m.routes, {
                    "id": route_id,
                    "job_id": "job-b" if route_id == "other-company" else "job-a",
                    "company_id": "company-b" if route_id == "other-company" else "company-a",
                    "provider": provider, "tenant": tenant, "requisition": req,
                    "url": f"https://board.example/jobs/{route_id}",
                })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def client(self):
        return TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        ))

    def test_reviewed_company_route_preference_reads_back_and_changes_only_same_rank_choice(self):
        with self.client() as client:
            saved = client.put(
                f"/profiles/{self.profile['id']}/companies/company-a/preferred-application-route",
                json={"route_id": "board-a", "preferred": True, "request_id": "prefer-board-a"},
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["preferred_route"]["provider"], "integrated-board-a")
            replay = client.put(
                f"/profiles/{self.profile['id']}/companies/company-a/preferred-application-route",
                json={"route_id": "board-a", "preferred": True, "request_id": "prefer-board-a"},
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), saved.json())
            readback = client.get(f"/jobs/job-a/routes?profile_id={self.profile['id']}")
            self.assertEqual(readback.status_code, 200, readback.text)

        routes = readback.json()["all_options"]
        trusted = [option["preferred_route"] for option in routes]
        plan = plan_application_routes(trusted, "DEFAULT")
        self.assertEqual(plan["selected"][0]["preferred_route"]["id"], "board-a")

    def test_route_preference_is_not_visible_to_another_profile(self):
        other = copy.deepcopy(PROFILE)
        other["id"] = "other-profile"
        self.store.save_profile(other)
        with self.client() as client:
            saved = client.put(
                f"/profiles/{self.profile['id']}/companies/company-a/preferred-application-route",
                json={"route_id": "board-a", "preferred": True, "request_id": "profile-one"},
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            readback = client.get(f"/jobs/job-a/routes?profile_id={other['id']}")
            self.assertEqual(readback.status_code, 200, readback.text)
        self.assertTrue(next(r for o in readback.json()["all_options"] for r in o["aliases"]
                             if r["id"] == "board-a")["preferred_by_profile"] is False)

    def test_user_can_clear_a_preferred_route_without_changing_other_route_preferences(self):
        with self.client() as client:
            for route_id, request_id, preferred in (
                ("board-a", "set-a", True), ("board-b", "set-b", True),
                ("board-a", "clear-a", False),
            ):
                response = client.put(
                    f"/profiles/{self.profile['id']}/companies/company-a/preferred-application-route",
                    json={"route_id": route_id, "preferred": preferred, "request_id": request_id},
                )
                self.assertEqual(response.status_code, 200, response.text)
            readback = client.get(f"/jobs/job-a/routes?profile_id={self.profile['id']}")
            self.assertEqual(readback.status_code, 200, readback.text)
        preferred = {r["id"] for option in readback.json()["all_options"] for r in option["aliases"]
                     if r["preferred_by_profile"]}
        self.assertEqual(preferred, {"board-b"})

    def test_reusing_request_id_for_another_route_conflicts_without_changing_preference(self):
        with self.client() as client:
            first = client.put(
                f"/profiles/{self.profile['id']}/companies/company-a/preferred-application-route",
                json={"route_id": "board-a", "preferred": True, "request_id": "stable-request"},
            )
            self.assertEqual(first.status_code, 200, first.text)
            conflict = client.put(
                f"/profiles/{self.profile['id']}/companies/company-a/preferred-application-route",
                json={"route_id": "board-b", "preferred": True, "request_id": "stable-request"},
            )
            self.assertEqual(conflict.status_code, 409)
            readback = client.get(f"/jobs/job-a/routes?profile_id={self.profile['id']}")
            self.assertEqual(readback.status_code, 200, readback.text)
        with self.store.engine.connect() as connection:
            event_count = connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.entity_id == self.profile["id"],
                m.audit.c.action == "APPLICATION_ROUTE_PREFERENCE",
            )).scalar_one()
        self.assertEqual(event_count, 1)
        route = next(r for o in readback.json()["all_options"] for r in o["aliases"]
                     if r["id"] == "board-a")
        self.assertTrue(route["preferred_by_profile"])

    def test_route_from_another_company_cannot_be_saved_under_this_company(self):
        with self.client() as client:
            response = client.put(
                f"/profiles/{self.profile['id']}/companies/company-a/preferred-application-route",
                json={"route_id": "other-company", "preferred": True, "request_id": "wrong-company"},
            )
        self.assertEqual(response.status_code, 404)

    def test_preferred_aggregator_does_not_out_rank_verified_ats_evidence(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.routes, {
                "id": "verified-ats", "job_id": "job-a", "company_id": "company-a",
                "provider": "greenhouse", "tenant": "example", "requisition": "req-a",
                "url": "https://boards.greenhouse.io/example/jobs/1",
            })
            upsert(connection, m.company_ats_tenants, {
                "id": "tenant-evidence", "company_id": "company-a", "provider": "greenhouse",
                "tenant_identifier": "example", "organization_identifier": "", "board_identifier": "",
                "confidence": 0.95, "evidence": ["PUBLIC_API_BOARD"],
                "first_seen_at": "2026-10-09T00:00:00Z", "last_seen_at": "2026-10-09T00:00:00Z",
            })
        with self.client() as client:
            saved = client.put(
                f"/profiles/{self.profile['id']}/companies/company-a/preferred-application-route",
                json={"route_id": "board-a", "preferred": True, "request_id": "trust-aggregator"},
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            readback = client.get(f"/jobs/job-a/routes?profile_id={self.profile['id']}")
            self.assertEqual(readback.status_code, 200, readback.text)
        routes = [route for option in readback.json()["all_options"] for route in option["aliases"]]
        plan = plan_application_routes(routes, "DEFAULT")
        self.assertEqual(plan["selected"][0]["preferred_route"]["id"], "verified-ats")
