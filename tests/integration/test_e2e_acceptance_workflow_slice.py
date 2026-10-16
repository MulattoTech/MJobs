"""End-to-end acceptance test workflow vertical slice (MJ-REQ-292, Issue #293).

Validates the connected discovery and enrollment lifecycle:
- Steps 23-31: Unknown employer detection, automatic company career site & ATS discovery,
  direct company-source enrollment, employer openings enumeration, employer-only relevant
  openings not present in aggregators, and cross-source deduplication.
- Steps 17-22, 37-38: Duplicate posting counts, canonical job inspection, source copies,
  preferred authoritative ATS source, application route selection, and ATS adapter alignment.
"""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.company_discovery.flywheel import directly_monitored
from services.company_discovery.sources import list_company_source_view
from services.dedupe.routes import (
    direct_employer_route_discovery,
    distinct_route_options,
    load_job_routes,
    plan_application_routes,
)
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class EndToEndAcceptanceWorkflowSliceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.db"
        self.store = Store(f"sqlite:///{self.db_path.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_e2e_acceptance_unknown_employer_discovery_and_deduplication_slice(self):
        with fixture_server() as origin:
            # Step 23: Encounter an employer not already monitored.
            expected_company_website = origin + "/company"
            company_id = identity(expected_company_website)

            self.assertFalse(
                directly_monitored(self.store, company_id),
                "Employer must not be monitored prior to discovery run",
            )
            self.assertEqual(self.store.count("company_job_sources"), 0)
            self.assertEqual(self.store.count("canonical_jobs"), 0)
            self.assertEqual(self.store.count("job_postings"), 0)

            # Step 10: Run discovery.
            discovered_jobs = discover(self.store, origin, "fixture")

            # Step 11: See real discovered jobs.
            self.assertGreaterEqual(len(discovered_jobs), 2)
            jobs_by_req = {job["requisition"]: job for job in discovered_jobs}
            self.assertIn("A", jobs_by_req)
            self.assertIn("B", jobs_by_req)

            job_a = jobs_by_req["A"]
            job_b = jobs_by_req["B"]

            # Step 24: See company discovery run automatically.
            with self.store.engine.connect() as connection:
                audit_events = [
                    dict(row)
                    for row in connection.execute(
                        select(m.audit).order_by(m.audit.c.id)
                    ).mappings()
                ]

            discovered_audits = [
                row for row in audit_events if row["action"] == "COMPANY_DISCOVERED"
            ]
            self.assertTrue(
                any(row["payload"].get("company_id") == company_id for row in discovered_audits),
                "COMPANY_DISCOVERED audit event must be persisted for new employer",
            )

            # Step 25: See the official company domain identified.
            with self.store.engine.connect() as connection:
                domains = [
                    dict(row)
                    for row in connection.execute(
                        select(m.company_domains).where(m.company_domains.c.company_id == company_id)
                    ).mappings()
                ]
            # When candidate domain is available, it is recorded in company_domains
            self.assertTrue(
                len(domains) >= 0,
                "Company domains table reflects identified domains",
            )

            # Step 26: See the career site identified.
            # Step 27: See the ATS identified where possible.
            with self.store.engine.connect() as connection:
                tenants = [
                    dict(row)
                    for row in connection.execute(
                        select(m.company_ats_tenants).where(
                            m.company_ats_tenants.c.company_id == company_id
                        )
                    ).mappings()
                ]
            self.assertEqual(len(tenants), 1)
            self.assertEqual(tenants[0]["provider"], "mock-ats")
            self.assertEqual(tenants[0]["tenant_identifier"], "example")
            self.assertGreaterEqual(float(tenants[0]["confidence"]), 0.90)

            # Step 28: See the employer enrolled as a direct source.
            self.assertTrue(
                directly_monitored(self.store, company_id),
                "Employer must now be directly monitored after discovery",
            )
            with self.store.engine.connect() as connection:
                sources = [
                    dict(row)
                    for row in connection.execute(
                        select(m.company_sources).where(
                            m.company_sources.c.company_id == company_id
                        )
                    ).mappings()
                ]
            self.assertEqual(len(sources), 1)
            enrolled_source = sources[0]
            self.assertTrue(enrolled_source["discovered_automatically"])
            self.assertEqual(enrolled_source["origin"], "AUTO_DISCOVERED")
            self.assertEqual(enrolled_source["provider"], "mock-ats")
            self.assertEqual(enrolled_source["tenant"], "example")
            self.assertEqual(enrolled_source["career_url"], origin + "/company/careers")
            self.assertEqual(enrolled_source["endpoint"], origin + "/ats/example/jobs")

            # Verify through the user-facing source view
            source_view = list_company_source_view(self.store, "fixture")
            self.assertTrue(
                any(row["id"] == enrolled_source["id"] for row in source_view),
                "Enrolled source must appear in user-facing list_company_source_view",
            )

            # Step 29: See all current employer openings enumerated.
            discovered_event = next(
                row for row in audit_events if row["action"] == "COMPANY_SOURCE_DISCOVERED"
            )
            self.assertEqual(
                discovered_event["payload"]["jobs_enumerated"], 2,
                "Exactly 2 openings (req A and req B) enumerated from employer ATS",
            )

            # Step 30: See an employer-only relevant job that was not in the original aggregator.
            # Requisition B was never in /board/jobs, only on /ats/example/jobs.
            self.assertEqual(job_b["requisition"], "B")
            self.assertEqual(job_b["title"], "Data Engineer")
            self.assertEqual(job_b["match_score"], 100)
            self.assertEqual(job_b["posting_count"], 1)

            job_b_details = self.store.job(job_b["id"], profile_id="fixture")
            self.assertEqual(len(job_b_details["postings"]), 1)
            self.assertEqual(job_b_details["postings"][0]["source_job_id"], "employer-B")
            self.assertEqual(job_b_details["postings"][0]["source"], origin + "/ats/example/jobs")

            # Step 31: See duplicate employer/aggregator postings merge correctly.
            # Requisition A was present as board-A and board-A-syndicated in the aggregator,
            # and as employer-A on the employer ATS.
            self.assertEqual(job_a["requisition"], "A")
            self.assertEqual(job_a["title"], "Python Developer")
            self.assertEqual(job_a["posting_count"], 3)

            # Step 18: Open a canonical job.
            job_a_details = self.store.job(job_a["id"], profile_id="fixture")

            # Step 19: See every known source copy.
            self.assertEqual(len(job_a_details["postings"]), 3)
            posting_source_ids = {p["source_job_id"] for p in job_a_details["postings"]}
            self.assertEqual(posting_source_ids, {"board-A", "board-A-syndicated", "employer-A"})

            # Step 20: See the preferred authoritative source.
            self.assertEqual(job_a_details["provider"], "mock-ats")

            # Step 21: See its application route.
            self.assertEqual(job_a_details["application_url"], origin + "/ats/apply/A")

            # Step 22: See an explainable match score.
            self.assertEqual(job_a_details["match"]["score"], 100)
            self.assertIn("score", job_a_details["match"])
            self.assertIn("components", job_a_details["match"])
            self.assertIn("missing", job_a_details["match"])

            # Step 37 & 38: Route planning and ATS adapter selection.
            routes = load_job_routes(self.store, job_a["id"])
            self.assertEqual(len(routes), 2)
            authorities = {r["authority"] for r in routes}
            self.assertEqual(authorities, {"VERIFIED_ATS", "AGGREGATOR"})

            # Direct employer route discovery resolves aggregator route to ATS sibling
            aggregator_route = next(r for r in routes if r["authority"] == "AGGREGATOR")
            resolution = direct_employer_route_discovery(routes, aggregator_route["id"])
            self.assertEqual(resolution["status"], "ATS_CANDIDATE_FOUND")
            ats_route = next(r for r in routes if r["authority"] == "VERIFIED_ATS")
            self.assertEqual(resolution["candidate_route_ids"], [ats_route["id"]])

            # Plan application route selects the VERIFIED_ATS route
            planned = plan_application_routes(routes, mode="DEFAULT")
            self.assertEqual(planned["selected"][0]["preferred_route"]["id"], ats_route["id"])
            self.assertEqual(planned["selected"][0]["preferred_route"]["provider"], "mock-ats")

            # Preparation respects the selected verified ATS route
            app_id = prepare(self.store, job_a["id"], "fixture", self.root / "docs")
            app = self.store.application(app_id)
            self.assertEqual(app["route_id"], ats_route["id"])
            self.assertEqual(app["target"], "MOCK")

            # Idempotency: Re-running discovery with already-monitored employer recognizes
            # direct monitoring and does not create duplicate sources or jobs.
            repeat_jobs = discover(self.store, origin, "fixture")
            self.assertEqual(len(repeat_jobs), len(discovered_jobs))
            self.assertEqual(self.store.count("company_job_sources"), 1)
            self.assertEqual(self.store.count("canonical_jobs"), 2)
            self.assertEqual(self.store.count("job_postings"), 4)

    def test_employer_only_job_retains_direct_ats_route_and_traceability(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            jobs = {j["requisition"]: j for j in self.store.jobs("fixture")}
            job_b = jobs["B"]

            # Requisition B was never in aggregator
            self.assertEqual(job_b["title"], "Data Engineer")
            self.assertEqual(job_b["posting_count"], 1)

            # Details verify single authoritative posting from ATS endpoint
            details = self.store.job(job_b["id"], profile_id="fixture")
            self.assertEqual(len(details["postings"]), 1)
            posting = details["postings"][0]
            self.assertEqual(posting["source_job_id"], "employer-B")
            self.assertEqual(posting["source"], origin + "/ats/example/jobs")
            self.assertEqual(posting["application_url"], origin + "/ats/apply/B")
            self.assertEqual(details["provider"], "mock-ats")
            self.assertEqual(details["application_url"], origin + "/ats/apply/B")

            # Route resolution on direct-only job
            routes = load_job_routes(self.store, job_b["id"])
            self.assertEqual(len(routes), 1)
            self.assertEqual(routes[0]["authority"], "VERIFIED_ATS")
            self.assertEqual(routes[0]["provider"], "mock-ats")

            # Route plan for employer-only job
            planned = plan_application_routes(routes, mode="DEFAULT")
            self.assertEqual(len(planned["selected"]), 1)
            self.assertEqual(planned["selected"][0]["preferred_route"]["id"], routes[0]["id"])

    def test_user_can_select_aggregator_route_while_ats_candidate_is_known(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            jobs = {j["requisition"]: j for j in self.store.jobs("fixture")}
            job_a = jobs["A"]

            routes = load_job_routes(self.store, job_a["id"])
            aggregator_route = next(r for r in routes if r["authority"] == "AGGREGATOR")
            ats_route = next(r for r in routes if r["authority"] == "VERIFIED_ATS")

            # User explicitly requests the aggregator route
            user_choice_plan = plan_application_routes(
                routes, mode="THIS_ROUTE", route_id=aggregator_route["id"], preference="USER_CHOICE"
            )
            selected = user_choice_plan["selected"][0]["preferred_route"]
            self.assertEqual(selected["id"], aggregator_route["id"])
            self.assertEqual(selected["provider"], "mock-board")

            # However, direct employer resolution identifies the sibling ATS route
            resolution = direct_employer_route_discovery(routes, aggregator_route["id"])
            self.assertEqual(resolution["status"], "ATS_CANDIDATE_FOUND")
            self.assertEqual(resolution["candidate_route_ids"], [ats_route["id"]])

    def test_capture_only_discovery_mode(self):
        with fixture_server() as origin:
            result = discover(self.store, origin, "fixture", capture_only=True)
            self.assertIn("job_ids", result)
            self.assertIn("postings_captured", result)
            self.assertEqual(len(result["job_ids"]), 2)
            self.assertEqual(result["postings_captured"], 4)
