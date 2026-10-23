"""Connected synthetic proof for the MJobs core design principle (MJ-REQ-298).

This is deliberately fixture-only evidence.  It exercises the existing public
service boundaries without contacting or submitting to a real employer.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.applications.mock_adapter import apply_mock
from services.applications.preflight import latest_preflight
from services.company_discovery.flywheel import directly_monitored
from services.company_discovery.sources import list_company_source_view
from services.dedupe.routes import load_job_routes, plan_application_routes
from services.discovery.pipeline import discover
from services.discovery.search_profiles import get_search_profile, save_search_profile
from services.documents.generate import prepare
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url
from tests.integration.test_spine import PROFILE


PROFILE_IDS = ("fixture", "uncertain", "auth", "captcha")


class CoreDesignPrincipleFlowTests(unittest.TestCase):
    """Prove one durable local flow; do not claim whole-product acceptance."""

    def _new_store(self) -> Store:
        self.database_url = f"sqlite:///{(self.root / 'core-design.db').as_posix()}"
        store = Store(self.database_url)
        store.migrate()
        return store

    def _reopen_store(self) -> Store:
        store = Store(self.database_url)
        store.migrate()
        return store

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-core-design-")
        self.root = Path(self.temp.name)
        self.store = self._new_store()
        self.store.bind_workspace_mode("FIXTURE")
        for profile_id in PROFILE_IDS:
            profile = deepcopy(PROFILE)
            profile.update({
                "id": profile_id,
                "name": f"Alex {profile_id.title()}",
                "email": f"{profile_id}@example.test",
            })
            self.store.save_profile(profile)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def _search_profile() -> dict:
        return {
            "id": "fixture-python-search",
            "profile_id": "fixture",
            "name": "Verified Python and SQL roles",
            "enabled": True,
            "include_titles": ["Python Developer", "Data Engineer"],
            "required_keywords": ["Python", "SQL"],
            "required_skills": ["Python", "SQL"],
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [],
        }

    def _prepare(self, profile_id: str, job_id: str) -> str:
        return prepare(self.store, job_id, profile_id, self.root)

    def _route(self, application_id: str) -> dict:
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.routes).where(
                m.routes.c.id == self.store.application(application_id)["route_id"]
            )).mappings().one())

    def _set_route_url(self, application_id: str, url: str) -> None:
        route = self._route(application_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == route["id"]).values(url=url))

    def test_verified_profile_to_safe_mock_application_survives_reopen(self):
        profile = self.store.profile("fixture")
        self.assertTrue(profile["facts"])
        self.assertTrue(all(fact["verified"] and fact["user_approved"]
                            for fact in profile["facts"]))
        self.assertTrue(all(answer["verified"] and answer["user_approved"]
                            for answer in profile["answers"]))

        search_id = save_search_profile(self.store, self._search_profile())
        search = get_search_profile(self.store, search_id)
        self.assertEqual(search["criteria"]["required_skills"], ["PYTHON", "SQL"])
        self.assertEqual(search["source_ids"], [])

        with fixture_server() as origin:
            for profile_id in PROFILE_IDS:
                discover(self.store, origin, profile_id)

            jobs = {row["requisition"]: row for row in self.store.jobs("fixture")}
            self.assertEqual(set(jobs), {"A", "B"})
            self.assertEqual(self.store.count("canonical_jobs"), 2)
            self.assertEqual(self.store.count("job_postings"), 4)

            company_id = identity(origin + "/company")
            self.assertTrue(directly_monitored(self.store, company_id))
            source_view = list_company_source_view(self.store, "fixture")
            direct_source = next(row for row in source_view if row["company_id"] == company_id)
            self.assertEqual(direct_source["provider"], "mock-ats")
            self.assertEqual(direct_source["tenant"], "example")
            self.assertTrue(direct_source["enabled"])
            with self.store.engine.connect() as connection:
                company_discovered = connection.execute(select(m.audit).where(
                    m.audit.c.action == "COMPANY_DISCOVERED",
                    m.audit.c.payload["company_id"].as_string() == company_id,
                )).mappings().one()
                tenant = dict(connection.execute(select(m.company_ats_tenants).where(
                    m.company_ats_tenants.c.company_id == company_id
                )).mappings().one())
            self.assertEqual(company_discovered["entity_id"], "fixture")
            self.assertEqual(tenant["provider"], "mock-ats")
            self.assertGreaterEqual(float(tenant["confidence"]), 0.90)

            canonical = jobs["A"]
            self.assertEqual(canonical["posting_count"], 3)
            details = self.store.job(canonical["id"], profile_id="fixture")
            self.assertEqual({row["source_job_id"] for row in details["postings"]},
                             {"board-A", "board-A-syndicated", "employer-A"})
            self.assertEqual(details["match"]["score"], 100)
            strengths = {
                row["skill"]: row["strength"]
                for row in details["match"]["components"]["requirements"]
            }
            self.assertEqual(strengths, {"Python": "REQUIRED", "SQL": "REQUIRED"})

            routes = load_job_routes(self.store, canonical["id"])
            route_plan = plan_application_routes(routes, mode="DEFAULT")
            preferred = route_plan["selected"][0]["preferred_route"]
            self.assertEqual(preferred["authority"], "VERIFIED_ATS")
            self.assertEqual(preferred["provider"], "mock-ats")

            application_ids = {
                profile_id: self._prepare(profile_id, canonical["id"])
                for profile_id in PROFILE_IDS
            }
            main_application = self.store.application(application_ids["fixture"])
            self.assertEqual(main_application["state"], "PREPARED")
            self.assertEqual(main_application["target"], "MOCK")
            self.assertEqual(main_application["route_id"], preferred["id"])
            self.assertEqual(main_application["fact_ids"], ["python", "sql"])

            dry_run = apply_mock(
                self.store, application_ids["fixture"], origin, self.root, submit=False,
                request_id="core-design-dry-run",
            )
            self.assertEqual(dry_run["state"], "DRY_RUN_COMPLETE")
            self.assertIsNone(dry_run["confirmation"])
            reviewed = latest_preflight(self.store, application_ids["fixture"])
            self.assertEqual(reviewed["result"], "PASS")
            self.assertTrue(reviewed["mappings"])

            requisition = self._route(application_ids["fixture"])["requisition"]
            self._set_route_url(
                application_ids["uncertain"],
                f"{origin}/ats/apply/{requisition}?dynamic=unknown",
            )
            uncertain = apply_mock(
                self.store, application_ids["uncertain"], origin, self.root, submit=False,
                request_id="core-design-uncertain",
            )
            self.assertEqual(uncertain["state"], "REVIEW_REQUIRED")
            self.assertIn("UNKNOWN_DYNAMIC_REQUIRED_FIELD", uncertain["trace"])

            self._set_route_url(
                application_ids["auth"],
                f"{origin}/ats/apply/{requisition}?auth_expired=true",
            )
            auth = apply_mock(
                self.store, application_ids["auth"], origin, self.root, submit=False,
                request_id="core-design-auth",
            )
            self.assertEqual(auth["state"], "AUTH_REQUIRED")
            self.assertIn("AUTH_REQUIRED", auth["trace"])

            self._set_route_url(
                application_ids["captcha"],
                f"{origin}/ats/apply/{requisition}?challenge=true",
            )
            captcha = apply_mock(
                self.store, application_ids["captcha"], origin, self.root, submit=False,
                request_id="core-design-captcha",
            )
            self.assertEqual(captcha["state"], "USER_INTERVENTION_REQUIRED")
            self.assertIn("CHALLENGE_DETECTED", captcha["trace"])

            counts = httpx.get(origin + "/_test/request-counts").json()
            self.assertEqual(counts["submit"], 0)

        self.store.engine.dispose()
        self.store = self._reopen_store()
        self.store.bind_workspace_mode("FIXTURE")

        self.assertEqual(get_search_profile(self.store, search_id)["profile_id"], "fixture")
        self.assertEqual(self.store.profile("fixture")["facts"], profile["facts"])
        self.assertEqual(self.store.count("canonical_jobs"), 2)
        self.assertEqual(self.store.count("job_postings"), 4)
        self.assertTrue(directly_monitored(self.store, company_id))
        self.assertEqual(self.store.application(application_ids["fixture"])["state"],
                         "DRY_RUN_COMPLETE")
        self.assertEqual(self.store.application(application_ids["uncertain"])["state"],
                         "REVIEW_REQUIRED")
        self.assertEqual(self.store.application(application_ids["auth"])["state"],
                         "AUTH_REQUIRED")
        self.assertEqual(self.store.application(application_ids["captcha"])["state"],
                         "USER_INTERVENTION_REQUIRED")
        self.assertEqual(self.store.count("applications"), 4)
        self.assertEqual(self.store.count("application_attempts"), 4)
        self.assertTrue(all(row["confirmation"] is None for row in self.store.applications()))


@unittest.skipUnless(
    os.environ.get("MJOBS_TEST_POSTGRES_URL"),
    "Set MJOBS_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database",
)
class CoreDesignPrincipleFlowPostgresTests(CoreDesignPrincipleFlowTests):
    """Run the same proof in one private PostgreSQL schema."""

    def _new_store(self) -> Store:
        url = _disposable_postgres_url(os.environ["MJOBS_TEST_POSTGRES_URL"])
        self.schema = "core_design_" + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self._drop_schema)
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.database_url = url.update_query_dict({
            "options": "-csearch_path=" + self.schema
            + " -clock_timeout=5000 -cstatement_timeout=60000",
        }).render_as_string(hide_password=False)
        store = Store(self.database_url)
        from packages.persistence import resume_workspace  # noqa: F401

        m.metadata.create_all(store.engine)
        return store

    def _reopen_store(self) -> Store:
        store = Store(self.database_url)
        m.metadata.create_all(store.engine)
        return store

    def _drop_schema(self) -> None:
        try:
            if (not self.schema.startswith("core_design_")
                    or len(self.schema) != 44
                    or any(c not in "0123456789abcdef" for c in self.schema[12:])):
                raise ValueError("Unexpected disposable schema identifier")
            with self.admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        finally:
            self.admin.dispose()


if __name__ == "__main__":
    unittest.main()
