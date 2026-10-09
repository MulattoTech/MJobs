"""Connected acceptance for isolated saved-search role-score recalculation."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.matching_explanations import create_matching_explanations_router
from apps.api.scoring_profiles import create_scoring_profiles_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.matching import build_explanation
from services.discovery.scheduler import schedule_due_search_profiles
from services.discovery.search_profiles import save_search_profile
from services.discovery.search_semantics import evaluate_search
from services.discovery.sources.collect import configure_source
from services.matching.profile_scoring import compose_profile_scoring
from services.matching.scoring_profiles import resolve_role_scoring_profile
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class RoleScoringPeerRecalculationIsolationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-peer-score-isolation-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'peer-scoring.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = deepcopy(PROFILE)
        profile["facts"] = [
            fact for fact in profile.get("facts", [])
            if "kubernetes" not in str(fact.get("text", "")).casefold()
        ]
        self.store.save_profile(profile)
        self.source_ids = {
            "systems-search": configure_source(
                self.store, "systems-peer-recalc", provider="remoteok", jitter_seconds=0,
            ),
            "cloud-search": configure_source(
                self.store, "cloud-peer-recalc", provider="remoteok", jitter_seconds=0,
            ),
        }
        self.endpoints = {
            "systems-search": "https://systems-source.example/jobs",
            "cloud-search": "https://cloud-source.example/jobs",
        }
        self._save_search("systems-search", "Systems Administrator", "Linux")
        self._save_search("cloud-search", "Cloud Engineer", "Kubernetes")
        self._seed_jobs()

        app = FastAPI()
        app.include_router(create_scoring_profiles_router(self.store))
        app.include_router(create_matching_explanations_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, title, required_keyword):
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
            "source_ids": [self.source_ids[search_id]],
            "scheduler": {"enabled": True, "interval_seconds": 300},
        })

    def _seed_jobs(self):
        rows = (
            ("systems-job", "systems-req", "Systems Administrator",
             "Linux systems administration", self.source_ids["systems-search"],
             self.endpoints["systems-search"], 20),
            ("cloud-job", "cloud-req", "Cloud Engineer",
             "Kubernetes platform operations", self.source_ids["cloud-search"],
             self.endpoints["cloud-search"], 80),
        )
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="synthetic-employer", name="Synthetic Infrastructure",
                domain="synthetic.example",
            ))
            for job_id, requisition, title, description, source_id, endpoint, ai_score in rows:
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id="synthetic-employer", requisition=requisition,
                    title=title, description=description, skills=["Kubernetes"],
                    location="Remote", remote_type="REMOTE",
                    attributes={
                        "source_id": source_id,
                        "source_endpoint": endpoint,
                        "specialized_signals": {"ai_intensity": {
                            "score": ai_score,
                            "provenance": {
                                "source": "synthetic_job_metadata",
                                "evidence_ref": f"{job_id}:ai-v1",
                            },
                        }},
                    },
                ))
                connection.execute(m.postings.insert().values(
                    id=f"posting-{job_id}", job_id=job_id, source=endpoint,
                    source_job_id=job_id, source_url=endpoint,
                    application_url=f"https://synthetic.example/jobs/{job_id}",
                    raw={"synthetic": True}, fetched_at="2026-10-09T00:00:00Z",
                ))

    @staticmethod
    def _definition(role_family, weights, thresholds):
        return {
            "schema_version": 1,
            "role_family": role_family,
            "weights": weights,
            "thresholds": thresholds,
        }

    def _profile_url(self, search_id):
        return f"/profiles/fixture/search-profiles/{search_id}/role-scoring-profile"

    def _explanation_url(self, search_id, job_id):
        return (f"/profiles/fixture/search-profiles/{search_id}/jobs/{job_id}"
                "/match-explanation")

    def _save_role_profile(self, search_id, role_family, weights, thresholds):
        response = self.client.put(self._profile_url(search_id), json={
            "expected_version": None,
            "profile": self._definition(role_family, weights, thresholds),
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["profile"]

    def _saved_search(self, search_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == search_id,
            )).mappings().one())

    def _match(self, job_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.matches).where(
                m.matches.c.job_id == job_id,
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == self.store.profile("fixture")["version"],
            )).mappings().one())

    def _hard_eligibility(self):
        with self.store.engine.connect() as connection:
            jobs = [dict(row) for row in connection.execute(select(m.jobs)).mappings()]
        return {
            search_id: {
                job["id"]: evaluate_search(self._saved_search(search_id)["criteria"], job)["matched"]
                for job in jobs
            }
            for search_id in ("systems-search", "cloud-search")
        }

    def _peer_audit_history(self, search_id):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.audit).where(
                m.audit.c.entity_id == search_id,
            ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings()]

    def _peer_operations(self, search_id):
        with self.store.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(select(m.operations)).mappings()]
        return [row for row in rows
                if isinstance(row["params"], dict)
                and row["params"].get("search_profile_id") == search_id]

    def _schedule_and_run(self, at):
        scheduled = schedule_due_search_profiles(self.store, at)
        operation_ids = [row["operation_id"] for row in scheduled["scheduled"]]
        environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
        }
        with patch("workers.tasks.collect_source"), \
                patch("workers.tasks._record_supported_collection_candidates"), \
                patch.dict(os.environ, environment):
            results = [execute_operation(operation_id) for operation_id in operation_ids]
        return scheduled, results

    def _set_ai_signal(self, score, evidence_ref):
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.jobs.c.attributes).where(
                m.jobs.c.id == "systems-job",
            )).scalar_one()
            attributes = deepcopy(row)
            attributes["specialized_signals"]["ai_intensity"] = {
                "score": score,
                "provenance": {"source": "synthetic_job_metadata",
                               "evidence_ref": evidence_ref},
            }
            connection.execute(update(m.jobs).where(
                m.jobs.c.id == "systems-job",
            ).values(attributes=attributes))

    def _expected_score(self, job_id, scoring_profile):
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

    def test_single_search_recalculation_preserves_peer_score_history_and_hard_eligibility(self):
        systems_profile = self._save_role_profile(
            "systems-search", "Systems Administration",
            {"skills": 20, "ai_intensity": 80}, {"ai_intensity": 90},
        )
        cloud_profile = self._save_role_profile(
            "cloud-search", "Cloud Operations",
            {"skills": 80, "ai_intensity": 20}, {"ai_intensity": 10},
        )
        self.assertNotEqual(systems_profile["weights"], cloud_profile["weights"])
        self.assertNotEqual(systems_profile["thresholds"], cloud_profile["thresholds"])

        start = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        initial, initial_results = self._schedule_and_run(start)
        self.assertEqual({row["search_profile_id"] for row in initial["scheduled"]},
                         {"systems-search", "cloud-search"})
        self.assertTrue(all(result["status"] == "COMPLETED" for result in initial_results),
                        initial_results)

        systems_before = self._match("systems-job")
        cloud_before = self._match("cloud-job")
        self.assertEqual(systems_before["score"],
                         self._expected_score("systems-job", systems_profile)["score"])
        self.assertEqual(cloud_before["score"],
                         self._expected_score("cloud-job", cloud_profile)["score"])
        self.assertEqual(systems_before["score"], 0.0)
        self.assertEqual(cloud_before["score"], 16.0)
        for search_id, job_id, configured in (
            ("systems-search", "systems-job", systems_profile),
            ("cloud-search", "cloud-job", cloud_profile),
        ):
            saved_profile = self.client.get(self._profile_url(search_id))
            self.assertEqual(saved_profile.status_code, 200, saved_profile.text)
            self.assertEqual(saved_profile.json()["profile"]["weights"],
                             configured["weights"])
            self.assertEqual(saved_profile.json()["profile"]["thresholds"],
                             configured["thresholds"])
            explanation = self.client.get(self._explanation_url(search_id, job_id))
            self.assertEqual(explanation.status_code, 200, explanation.text)
            self.assertEqual(explanation.json()["scoring_profile"]["version"],
                             configured["version"])

        eligible_before = self._hard_eligibility()
        cloud_history_before = self._peer_audit_history("cloud-search")
        cloud_operations_before = self._peer_operations("cloud-search")
        cloud_search_before = self._saved_search("cloud-search")

        updated_response = self.client.put(self._profile_url("systems-search"), json={
            "expected_version": systems_profile["version"],
            "profile": self._definition(
                "Systems Administration", {"skills": 0, "ai_intensity": 100},
                {"ai_intensity": 10},
            ),
        })
        self.assertEqual(updated_response.status_code, 200, updated_response.text)
        updated_profile = updated_response.json()["profile"]
        self._set_ai_signal(50, "systems-job:ai-v2")

        # Hold the peer source outside its poll window so only the systems search is due.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == self.source_ids["cloud-search"],
            ).values(last_attempt="2099-01-01T00:00:00+00:00"))
        recalculated, results = self._schedule_and_run(start + timedelta(seconds=301))
        self.assertEqual([row["search_profile_id"] for row in recalculated["scheduled"]],
                         ["systems-search"])
        self.assertTrue(all(result["status"] == "COMPLETED" for result in results), results)

        systems_after = self._match("systems-job")
        cloud_after = self._match("cloud-job")
        self.assertEqual(systems_after["score"], 50.0)
        self.assertNotEqual(systems_after["score"], systems_before["score"])
        self.assertEqual(systems_after["score"],
                         self._expected_score("systems-job", updated_profile)["score"])
        self.assertEqual(cloud_after, cloud_before)
        self.assertEqual(self._peer_audit_history("cloud-search"), cloud_history_before)
        self.assertEqual(self._peer_operations("cloud-search"), cloud_operations_before)
        self.assertEqual(self._saved_search("cloud-search"), cloud_search_before)
        self.assertEqual(self._hard_eligibility(), eligible_before)

        selected_explanation = self.client.get(
            self._explanation_url("systems-search", "systems-job"),
        )
        self.assertEqual(selected_explanation.status_code, 200, selected_explanation.text)
        self.assertEqual(selected_explanation.json()["scoring_profile"]["version"],
                         updated_profile["version"])
        updated_readback = self.client.get(self._profile_url("systems-search"))
        self.assertEqual(updated_readback.status_code, 200, updated_readback.text)
        self.assertEqual(updated_readback.json()["profile"]["weights"],
                         updated_profile["weights"])
        self.assertEqual(updated_readback.json()["profile"]["thresholds"],
                         updated_profile["thresholds"])


if __name__ == "__main__":
    unittest.main()
