import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.dedupe.routes import (
    direct_employer_route_discovery,
    distinct_route_options,
    load_job_routes,
    list_route_choices,
    plan_application_routes,
    route_underlying_key,
    source_provenance_inventory,
)
from services.dedupe.route_redirects import RouteRedirectProbeClient
from services.documents.generate import PreparationError, prepare
from tests.integration.test_spine import PROFILE


class ApplicationRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        route_profile = dict(PROFILE, facts=[*PROFILE["facts"],
            {"id": "windows", "kind": "skill", "text": "Windows", "source": "route test profile",
             "verified": True, "user_approved": True, "confidence": 100}])
        self.store.save_profile(route_profile)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": "company", "name": "Example", "domain": "example.test"})
            upsert(connection, m.jobs, {
                "id": "job", "company_id": "company", "requisition": "CANON",
                "title": "Systems Administrator", "description": "Responsibilities: Maintain Windows systems.",
                "skills": ["Windows"], "location": "Dallas, TX", "remote_type": "HYBRID",
                "attributes": {},
            })
            routes = [
                {"id": "agg-a", "job_id": "job", "company_id": "company", "provider": "job-board",
                 "tenant": "board", "requisition": "REQ-A", "url": "https://redirect.example/apply/a"},
                {"id": "ats-a", "job_id": "job", "company_id": "company", "provider": "greenhouse",
                 "tenant": "example", "requisition": "REQ-A", "url": "https://job-boards.greenhouse.io/example/jobs/1"},
                {"id": "ats-b", "job_id": "job", "company_id": "company", "provider": "lever",
                 "tenant": "global:example", "requisition": "REQ-B",
                 "url": "https://jobs.lever.co/example/00000000-0000-0000-0000-000000000002"},
            ]
            for route in routes:
                upsert(connection, m.routes, route)
            for provider, tenant in (("greenhouse", "example"), ("lever", "global:example")):
                upsert(connection, m.company_ats_tenants, {
                    "id": f"verified-{provider}", "company_id": "company", "provider": provider,
                    "tenant_identifier": tenant, "organization_identifier": "", "board_identifier": "",
                    "confidence": 0.94, "evidence": ["PUBLIC_API_BOARD"],
                    "first_seen_at": "2026-10-09T00:00:00Z", "last_seen_at": "2026-10-09T00:00:00Z",
                })
            for identifier, source, source_job_id, application_url in (
                ("p1", "board-a", "1", "https://redirect.example/apply/a"),
                ("p2", "greenhouse:example", "1", "https://job-boards.greenhouse.io/example/jobs/1"),
                ("p3", "lever:global:example", "00000000-0000-0000-0000-000000000002",
                 "https://jobs.lever.co/example/00000000-0000-0000-0000-000000000002"),
            ):
                upsert(connection, m.postings, {
                    "id": identifier, "job_id": "job", "source": source,
                    "source_job_id": source_job_id, "source_url": source,
                    "application_url": application_url, "raw": {"source": source},
                    "fetched_at": "2026-10-01T12:00:00Z",
                })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def routes(self):
        return load_job_routes(self.store, "job")

    def test_same_employer_requisition_collapses_redirect_aliases(self):
        options = distinct_route_options(self.routes())
        self.assertEqual(len(options), 2)
        req_a = next(option for option in options if option["underlying_key"].endswith(":req-a"))
        self.assertEqual(req_a["preferred_route"]["id"], "ats-a")
        self.assertEqual({row["id"] for row in req_a["aliases"]}, {"agg-a", "ats-a"})
        self.assertEqual(route_underlying_key(req_a["aliases"][0]), "employer:company:req-a")

    def test_route_modes_select_this_employer_or_all_distinct(self):
        routes = self.routes()
        this_route = plan_application_routes(routes, "THIS_ROUTE", "agg-a")
        self.assertEqual(len(this_route["selected"]), 1)
        self.assertEqual(this_route["selected"][0]["underlying_key"], "employer:company:req-a")

        employer = plan_application_routes(routes, "EMPLOYER_SITE")
        self.assertEqual(len(employer["selected"]), 1)
        self.assertIn(employer["selected"][0]["preferred_route"]["provider"], {"greenhouse", "lever"})

        all_distinct = plan_application_routes(routes, "ALL_DISTINCT")
        self.assertEqual(all_distinct["distinct_count"], 2)
        self.assertEqual(len(all_distinct["selected"]), 2)

    def test_profile_policy_can_require_an_explicit_route_without_disabling_explicit_selection(self):
        routes = self.routes()
        with self.assertRaisesRegex(ValueError, "APPLICATION_ROUTE_SELECTION_REQUIRED"):
            plan_application_routes(routes, preference="USER_CHOICE")
        selected = plan_application_routes(routes, "THIS_ROUTE", "agg-a", preference="USER_CHOICE")
        self.assertEqual(selected["selected"][0]["preferred_route"]["id"], "agg-a")

    def test_known_ats_route_is_not_preferred_without_exact_verified_tenant_evidence(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.company_ats_tenants.delete().where(
                m.company_ats_tenants.c.provider == "greenhouse"))
        greenhouse = next(route for route in self.routes() if route["id"] == "ats-a")
        self.assertEqual(greenhouse["authority"], "UNKNOWN")
        option = next(option for option in distinct_route_options(self.routes())
                      if option["underlying_key"].endswith(":req-a"))
        self.assertEqual(option["preferred_route"]["id"], "agg-a")

    def test_aggregator_route_discovery_returns_verified_ats_sibling_without_replacing_user_choice(self):
        routes = self.routes()
        resolution = direct_employer_route_discovery(routes, "agg-a")
        self.assertEqual(resolution, {
            "status": "ATS_CANDIDATE_FOUND",
            "source_route_id": "agg-a",
            "candidate_route_ids": ["ats-a"],
        })
        selected = plan_application_routes(routes, "THIS_ROUTE", "agg-a")
        self.assertEqual(selected["selected"][0]["preferred_route"]["id"], "agg-a")

    def test_aggregator_route_discovery_stays_unresolved_without_verified_ats_sibling(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.company_ats_tenants.delete().where(
                m.company_ats_tenants.c.provider == "greenhouse"))
        self.assertEqual(direct_employer_route_discovery(self.routes(), "agg-a"), {
            "status": "UNRESOLVED",
            "source_route_id": "agg-a",
            "candidate_route_ids": [],
        })

    def test_aggregator_route_discovery_surfaces_multiple_verified_ats_candidates_as_ambiguous(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.routes, {
                "id":"ats-alt", "job_id":"job", "company_id":"company",
                "provider":"lever", "tenant":"global:employer", "requisition":"req-a",
                "url":"https://jobs.lever.co/employer/00000000-0000-0000-0000-000000000001",
            })
            upsert(connection, m.company_ats_tenants, {
                "id":"tenant-alt", "company_id":"company", "provider":"lever",
                "tenant_identifier":"global:employer", "organization_identifier":"",
                "board_identifier":"", "confidence":0.91, "evidence":["PUBLIC_API_BOARD"],
                "first_seen_at":"2026-10-09T00:00:00Z", "last_seen_at":"2026-10-09T00:00:00Z",
            })
        self.assertEqual(direct_employer_route_discovery(self.routes(), "agg-a"), {
            "status": "AMBIGUOUS", "source_route_id": "agg-a",
            "candidate_route_ids": ["ats-a", "ats-alt"],
        })

    def test_prepare_all_distinct_persists_one_choice_per_underlying_route(self):
        application_id = prepare(
            self.store, "job", "fixture", self.root,
            route_mode="ALL_DISTINCT",
        )
        choices = list_route_choices(self.store, application_id)
        self.assertEqual(len(choices), 2)
        self.assertEqual({row["underlying_key"] for row in choices},
                         {"employer:company:req-a", "employer:company:req-b"})
        self.assertEqual(self.store.count("job_postings"), 3)
        self.assertEqual(self.store.count("canonical_jobs"), 1)

    def test_document_worker_enforces_user_route_choice_and_persists_the_selected_route(self):
        profile = self.store.profile("fixture")
        profile["preferences"]["application_route_preference"] = "USER_CHOICE"
        self.store.save_profile(profile)
        with self.assertRaisesRegex(PreparationError, "Choose an application route"):
            prepare(self.store, "job", "fixture", self.root)
        application_id = prepare(
            self.store, "job", "fixture", self.root,
            route_mode="THIS_ROUTE", route_id="agg-a",
        )
        choices = list_route_choices(self.store, application_id)
        self.assertEqual([choice["route_id"] for choice in choices], ["agg-a"])

    def test_job_read_model_keeps_all_routes_and_postings_without_duplicate_list_rows(self):
        rows = self.store.jobs("fixture")
        self.assertEqual(len(rows), 1)
        detail = self.store.job("job", "fixture")
        self.assertEqual(len(detail["routes"]), 3)
        self.assertEqual(len(detail["postings"]), 3)

    def test_route_endpoint_exposes_distinct_options_and_every_source_record(self):
        with TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        )) as client:
            response = client.get("/jobs/job/routes")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["distinct_count"], 2)
        self.assertEqual(len(body["source_records"]), 3)
        self.assertEqual({row["posting_id"] for row in body["source_records"]}, {"p1", "p2", "p3"})
        aggregator = next(route for option in body["all_options"] for route in option["aliases"]
                          if route["id"] == "agg-a")
        self.assertEqual(aggregator["direct_employer_resolution"], {
            "status": "ATS_CANDIDATE_FOUND", "source_route_id": "agg-a", "candidate_route_ids": ["ats-a"],
        })

    def test_explicit_route_check_uses_company_evidence_and_does_not_change_saved_routes(self):
        def request_once(client, url, deadline):
            request = httpx.Request("GET", url)
            if url == "https://redirect.example/apply/a":
                return httpx.Response(302, headers={
                    "location":"https://boards.greenhouse.io/example/jobs/1?tracking=private",
                }, request=request)
            return httpx.Response(200, request=request)

        with patch.object(RouteRedirectProbeClient, "_load_robots"), \
             patch.object(RouteRedirectProbeClient, "_request_once", request_once), \
             patch("services.company_discovery.public_client.MIN_REQUEST_INTERVAL", 0), \
             TestClient(create_app(
                self.store, "http://127.0.0.1:8090", self.root,
                "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
             )) as client:
            response = client.post("/jobs/job/routes/agg-a/resolve")
            saved_route_count = self.store.count("application_routes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "RESOLVED_ATS_ROUTE")
        self.assertEqual(response.json()["source_route_id"], "agg-a")
        self.assertEqual(response.json()["candidate_url"], "https://boards.greenhouse.io/example/jobs/1")
        self.assertEqual(response.json()["tenant"], "example")
        self.assertEqual(saved_route_count, 3)

    def test_explicit_route_check_rejects_a_verified_route_without_network_access(self):
        with patch("apps.api.main.resolve_aggregator_redirect") as resolve, TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        )) as client:
            response = client.post("/jobs/job/routes/ats-a/resolve")
        self.assertEqual(response.status_code, 409)
        resolve.assert_not_called()

    def test_api_requires_explicit_route_when_profile_asks_for_choice(self):
        job_id = "a" * 64
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id":"route-policy-company","name":"Route Policy Co","domain":"routepolicy.example"})
            upsert(connection, m.jobs, {
                "id":job_id,"company_id":"route-policy-company","requisition":"route-policy",
                "title":"Systems Engineer","description":"Support systems.","skills":["Python"],
                "location":"Remote","remote_type":"REMOTE","attributes":{},
            })
            upsert(connection, m.routes, {
                "id":"route-policy-aggregator","job_id":job_id,"company_id":"route-policy-company",
                "provider":"job-board","tenant":"board","requisition":"route-policy",
                "url":"https://board.example/jobs/route-policy",
            })
            upsert(connection, m.routes, {
                "id":"route-policy-ats","job_id":job_id,"company_id":"route-policy-company",
                "provider":"greenhouse","tenant":"routepolicy","requisition":"route-policy",
                "url":"https://job-boards.greenhouse.io/routepolicy/jobs/1",
            })
            upsert(connection, m.company_ats_tenants, {
                "id":"route-policy-verified-ats","company_id":"route-policy-company",
                "provider":"greenhouse","tenant_identifier":"routepolicy",
                "organization_identifier":"","board_identifier":"","confidence":0.94,
                "evidence":["PUBLIC_API_BOARD"],"first_seen_at":"2026-10-09T00:00:00Z",
                "last_seen_at":"2026-10-09T00:00:00Z",
            })
        profile = self.store.profile("fixture")
        profile["preferences"]["application_route_preference"] = "USER_CHOICE"
        self.store.save_profile(profile)
        with TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        )) as client:
            body = {"profile_id":"fixture", "job_id":job_id, "request_id":"explicit-choice"}
            denied = client.post("/applications", json=body)
            self.assertEqual(denied.status_code, 409, denied.text)
            self.assertEqual(denied.json()["detail"], "APPLICATION_ROUTE_SELECTION_REQUIRED")
            selected = client.post("/applications", json={
                **body, "request_id":"explicit-route", "route_mode":"THIS_ROUTE", "route_id":"route-policy-aggregator",
            })
            self.assertEqual(selected.status_code, 202, selected.text)
            self.assertEqual(selected.json()["route_ids"], ["route-policy-aggregator"])

    def test_job_detail_marks_only_evidence_qualified_routes_as_verified(self):
        with TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        )) as client:
            response = client.get("/jobs/job", params={"profile_id":"fixture"})
        self.assertEqual(response.status_code, 200, response.text)
        routes = {route["id"]:route for route in response.json()["routes"]}
        self.assertEqual(routes["ats-a"]["authority"], "VERIFIED_ATS")
        self.assertEqual(routes["agg-a"]["authority"], "AGGREGATOR")
        self.assertEqual(routes["agg-a"]["direct_employer_resolution"]["candidate_route_ids"], ["ats-a"])

    def test_source_inventory_never_collapses_records(self):
        postings = [
            {"id": "a", "source": "board", "source_job_id": "1", "source_url": "u1",
             "application_url": "a1", "fetched_at": "t1"},
            {"id": "b", "source": "employer", "source_job_id": "1", "source_url": "u2",
             "application_url": "a2", "fetched_at": "t2"},
        ]
        inventory = source_provenance_inventory(postings)
        self.assertEqual([row["posting_id"] for row in inventory], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
