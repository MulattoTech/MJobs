"""Red-first acceptance for selected-search scheduled role-score recalculation.

The fixture uses the durable saved-search scheduler and actual DISCOVER worker
path with source collection replaced by a no-network stub. Profile CRUD and
explanation reads use the real router factories. Current main does not yet
consume criteria.role_scoring_profile from recalculate; the expected failure
marks that worker/service integration dependency without weakening the oracle.
"""

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.matching_explanations import create_matching_explanations_router
from apps.api.scoring_profiles import create_scoring_profiles_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.matching import build_explanation
from services.discovery.scheduler import schedule_due_search_profiles
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import configure_source
from services.matching.profile_scoring import compose_profile_scoring
from services.matching.scoring_profiles import resolve_role_scoring_profile
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class SelectedSearchRecalculationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-selected-role-rescore-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'selected-rescore.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.profile = deepcopy(PROFILE)
        self.profile["facts"] = [
            fact for fact in self.profile.get("facts", [])
            if "kubernetes" not in str(fact.get("text", "")).casefold()
        ]
        self.store.save_profile(self.profile)
        self.source_one = configure_source(
            self.store, "selected-rescore-one", provider="remoteok", jitter_seconds=0,
        )
        self.source_two = configure_source(
            self.store, "selected-rescore-two", provider="remoteok", jitter_seconds=0,
        )
        self.endpoint_one = "https://source-one.example/jobs"
        self.endpoint_two = "https://source-two.example/jobs"
        self._save_search(
            "systems-search", self.source_one, "Systems Administrator", "Linux",
        )
        self._save_search(
            "cloud-search", self.source_two, "Cloud Engineer", "Kubernetes",
        )
        self._seed_jobs_and_postings()

        app = FastAPI()
        app.include_router(create_scoring_profiles_router(self.store))
        app.include_router(create_matching_explanations_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, source_id, title, required_keyword):
        save_search_profile(self.store, {
            "id": search_id,
            "profile_id": "fixture",
            "name": search_id,
            "enabled": True,
            "include_titles": [title],
            "exclude_titles": [],
            "required_keywords": [required_keyword],
            "optional_keywords": [],
            "excluded_keywords": [],
            "exact_phrases": [],
            "technology_aliases": {},
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [source_id],
            "scheduler": {"enabled": True, "interval_seconds": 300},
        })

    def _seed_jobs_and_postings(self):
        rows = (
            ("systems-job", "systems-req", "Systems Administrator",
             "Linux systems administration", ["Kubernetes"], self.source_one,
             self.endpoint_one, 20),
            ("cloud-job", "cloud-req", "Cloud Engineer",
             "Kubernetes platform operations", ["Kubernetes"], self.source_two,
             self.endpoint_two, 80),
        )
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="synthetic-employer", name="Synthetic Infrastructure",
                domain="synthetic.example",
            ))
            for (job_id, requisition, title, description, skills, source_id,
                 endpoint, ai_score) in rows:
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id="synthetic-employer", requisition=requisition,
                    title=title, description=description, skills=skills,
                    location="Remote", remote_type="REMOTE",
                    attributes={"source_id": source_id, "source_endpoint": endpoint,
                                "specialized_signals": {"ai_intensity": {
                                    "score": ai_score,
                                    "provenance": {
                                        "source": "synthetic_job_metadata",
                                        "evidence_ref": f"{job_id}:ai-intensity",
                                    },
                                }}},
                ))
                connection.execute(m.postings.insert().values(
                    id=f"posting-{job_id}", job_id=job_id, source=endpoint,
                    source_job_id=job_id, source_url=endpoint,
                    application_url=f"https://synthetic.example/jobs/{job_id}",
                    raw={"synthetic": True}, fetched_at="2026-10-09T00:00:00Z",
                ))

    @staticmethod
    def _definition(role_family, weights):
        return {
            "schema_version": 1,
            "role_family": role_family,
            "weights": weights,
            "thresholds": {},
        }

    def _profile_url(self, search_id):
        return f"/profiles/fixture/search-profiles/{search_id}/role-scoring-profile"

    def _explanation_url(self, search_id, job_id):
        return (f"/profiles/fixture/search-profiles/{search_id}/jobs/{job_id}"
                "/match-explanation")

    def _save_role_profile(self, search_id, role_family, weights):
        response = self.client.put(self._profile_url(search_id), json={
            "expected_version": None,
            "profile": self._definition(role_family, weights),
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["profile"]

    def _saved_search(self, search_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == search_id,
            )).mappings().one())

    def _match(self, job_id):
        profile = self.store.profile("fixture")
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.matches).where(
                m.matches.c.job_id == job_id,
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == profile["version"],
            )).mappings().one())

    def _expected_profiled_score(self, job_id, scoring_profile):
        profile = self.store.profile("fixture")
        with self.store.engine.connect() as connection:
            job = dict(connection.execute(select(m.jobs).where(
                m.jobs.c.id == job_id,
            )).mappings().one())
        legacy = build_explanation(job, profile)
        resolution = resolve_role_scoring_profile(
            scoring_profile["role_family"],
            {scoring_profile["role_family"]: scoring_profile},
        )
        return compose_profile_scoring(job, legacy, resolution)

    def test_scheduled_discover_uses_selected_search_weights_and_keeps_peer_search_isolated(self):
        systems_profile = self._save_role_profile(
            "systems-search", "Systems Administration",
            {"skills": 20, "ai_intensity": 80, "cloud_experience": 15},
        )
        cloud_profile = self._save_role_profile(
            "cloud-search", "Cloud Operations",
            {"skills": 75, "ai_intensity": 25, "cloud_experience": 15},
        )
        self.assertEqual(self.client.get(self._profile_url("systems-search")).json()["profile"],
                         systems_profile)
        self.assertEqual(self.client.get(self._profile_url("cloud-search")).json()["profile"],
                         cloud_profile)

        scheduled = schedule_due_search_profiles(
            self.store, datetime(2026, 10, 9, 12, tzinfo=timezone.utc),
        )
        self.assertEqual({row["search_profile_id"] for row in scheduled["scheduled"]},
                         {"systems-search", "cloud-search"})
        with self.store.engine.connect() as connection:
            operations = [dict(row) for row in connection.execute(select(m.operations).where(
                m.operations.c.kind == "DISCOVER",
            )).mappings()]
        operations_by_search = {
            row["params"]["search_profile_id"]: row for row in operations
        }
        self.assertEqual(set(operations_by_search), {"systems-search", "cloud-search"})
        self.assertEqual(operations_by_search["systems-search"]["params"]["source_id"],
                         self.source_one)
        self.assertEqual(operations_by_search["cloud-search"]["params"]["source_id"],
                         self.source_two)

        environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
        }
        with patch("workers.tasks.collect_source"), \
                patch("workers.tasks._record_supported_collection_candidates"), \
                patch.dict(os.environ, environment):
            for search_id in ("systems-search", "cloud-search"):
                result = execute_operation(operations_by_search[search_id]["id"])
                self.assertEqual(result["status"], "COMPLETED", result)

        profile_version = self.store.profile("fixture")["version"]
        with self.store.engine.connect() as connection:
            persisted_job_ids = set(connection.execute(select(m.matches.c.job_id).where(
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == profile_version,
            )).scalars())
        self.assertEqual(persisted_job_ids, {"systems-job", "cloud-job"})

        for search_id, job_id, configured in (
            ("systems-search", "systems-job", systems_profile),
            ("cloud-search", "cloud-job", cloud_profile),
        ):
            with self.subTest(search_id=search_id):
                match = self._match(job_id)
                expected = self._expected_profiled_score(job_id, configured)
                self.assertEqual({
                    "score": match["score"],
                    "scoring_profile": match["components"].get("scoring_profile"),
                    "scoring_contributions": match["components"].get("scoring_contributions"),
                }, {
                    "score": expected["score"],
                    "scoring_profile": expected["scoring_profile"],
                    "scoring_contributions": expected["scoring_contributions"],
                })

                response = self.client.get(self._explanation_url(search_id, job_id))
                self.assertEqual(response.status_code, 200, response.text)
                explanation = response.json()
                self.assertEqual(explanation["search_profile_id"], search_id)
                self.assertEqual(explanation["scoring_profile"]["id"], configured["id"])
                self.assertEqual(explanation["scoring_profile"]["version"], configured["version"])
                by_dimension = {row["dimension"]: row
                                for row in explanation["scoring_contributions"]}
                self.assertEqual(by_dimension["ai_intensity"]["provenance"]["source"],
                                 "synthetic_job_metadata")
                self.assertIsNone(by_dimension["cloud_experience"]["raw_score"])
                self.assertEqual(by_dimension["cloud_experience"]["provenance"],
                                 {"source": "UNKNOWN"})

        systems_search = self._saved_search("systems-search")
        cloud_search = self._saved_search("cloud-search")
        systems_saved = systems_search["criteria"]["role_scoring_profile"]
        cloud_saved = cloud_search["criteria"]["role_scoring_profile"]
        self.assertEqual(systems_saved["id"], systems_profile["id"])
        self.assertEqual(cloud_saved["id"], cloud_profile["id"])
        self.assertNotEqual(systems_saved["id"], cloud_saved["id"])
        self.assertEqual(systems_search["criteria"]["required_keywords"], ["Linux"])
        self.assertEqual(cloud_search["criteria"]["required_keywords"], ["Kubernetes"])


if __name__ == "__main__":
    unittest.main()
