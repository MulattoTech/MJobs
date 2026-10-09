"""Fail-closed and recovery acceptance for scheduled selected-search scoring."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.scheduler import schedule_due_search_profiles
from services.discovery.search_profiles import save_search_profile
from services.discovery.search_semantics import evaluate_search
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class SelectedSearchScoringFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-selected-score-fail-closed-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'selected-scoring.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.profile = deepcopy(PROFILE)
        self.profile["facts"] = [
            fact for fact in self.profile.get("facts", [])
            if "kubernetes" not in str(fact.get("text", "")).casefold()
        ]
        self.store.save_profile(self.profile)
        self.source_id = configure_source(
            self.store, "role-score-fail-closed", provider="remoteok", jitter_seconds=0,
        )
        self.endpoint = "https://synthetic-source.example/jobs"
        save_search_profile(self.store, {
            "id": "selected-search",
            "profile_id": "fixture",
            "name": "Selected Systems Search",
            "enabled": True,
            "include_titles": ["Systems Administrator"],
            "exclude_titles": [],
            "required_keywords": ["Linux"],
            "optional_keywords": [],
            "excluded_keywords": ["temporary contract"],
            "exact_phrases": [],
            "technology_aliases": {},
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [self.source_id],
            "scheduler": {"enabled": True, "interval_seconds": 300},
        })
        self._seed_jobs()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))
        self.addCleanup(self.client.close)

    def _seed_jobs(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="synthetic-employer", name="Synthetic Infrastructure",
                domain="synthetic.example",
            ))
            connection.execute(m.jobs.insert().values(
                id="systems-job", company_id="synthetic-employer", requisition="systems-1",
                title="Systems Administrator", description="Linux systems administration",
                skills=["Kubernetes"], location="Remote", remote_type="REMOTE",
                attributes={"source_id": self.source_id,
                            "source_endpoint": self.endpoint,
                            "specialized_signals": {"ai_intensity": {
                                "score": 20,
                                "provenance": {"source": "synthetic_job_metadata",
                                               "evidence_ref": "systems-job:ai-v1"},
                            }}},
            ))
            connection.execute(m.jobs.insert().values(
                id="hard-excluded-job", company_id="synthetic-employer",
                requisition="other-1", title="Account Executive",
                description="Temporary contract sales role", skills=[],
                location="Remote", remote_type="REMOTE",
                attributes={"source_id": self.source_id,
                            "source_endpoint": self.endpoint},
            ))
            for job_id in ("systems-job", "hard-excluded-job"):
                connection.execute(m.routes.insert().values(
                    id=f"route-{job_id}", job_id=job_id,
                    company_id="synthetic-employer", provider="GREENHOUSE",
                    tenant="synthetic-board", requisition=job_id,
                    url=f"https://synthetic.example/apply/{job_id}",
                ))
                connection.execute(m.postings.insert().values(
                    id=f"posting-{job_id}", job_id=job_id, source=self.endpoint,
                    source_job_id=job_id, source_url=self.endpoint,
                    application_url=f"https://synthetic.example/jobs/{job_id}",
                    raw={"synthetic": True}, fetched_at="2026-10-09T00:00:00Z",
                ))

    @staticmethod
    def _definition():
        return {
            "schema_version": 1,
            "role_family": "Systems Administration",
            "weights": {"skills": 0, "ai_intensity": 100, "cloud_experience": 10},
            "thresholds": {},
        }

    def _profile_url(self):
        return "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"

    def _explanation_url(self):
        return ("/profiles/fixture/search-profiles/selected-search/jobs/systems-job/"
                "match-explanation")

    def _search(self):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            )).mappings().one())

    def _matches(self):
        profile_version = self.store.profile("fixture")["version"]
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.matches).where(
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == profile_version,
            ).order_by(m.matches.c.job_id)).mappings()]

    def _visible_search_job_ids(self):
        response = self.client.get("/jobs", params={
            "profile_id": "fixture", "search_profile_id": "selected-search",
            "sort": "title", "limit": 50,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return {row["id"] for row in response.json()["jobs"]}

    def _hard_eligibility(self):
        criteria = self._search()["criteria"]
        with self.store.engine.connect() as connection:
            jobs = [dict(row) for row in connection.execute(select(m.jobs)).mappings()]
        return {
            row["id"]: evaluate_search(criteria, row)["matched"]
            for row in jobs
        }

    def _schedule_and_run(self, at):
        scheduled = schedule_due_search_profiles(self.store, at)
        self.assertEqual([row["search_profile_id"] for row in scheduled["scheduled"]],
                         ["selected-search"])
        operation_id = scheduled["scheduled"][0]["operation_id"]
        environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
        }
        with patch("workers.tasks.collect_source"), \
                patch("workers.tasks._record_supported_collection_candidates"), \
                patch.dict(os.environ, environment):
            result = execute_operation(operation_id)
        return operation_id, result

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

    def _corrupt_profile(self, kind):
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            )).mappings().one()
            criteria = deepcopy(row["criteria"])
            profile = criteria["role_scoring_profile"]
            if kind == "unsupported_schema":
                profile["schema_version"] = 2
            elif kind == "negative_weight":
                profile["weights"]["ai_intensity"] = -1
            else:
                self.fail(f"Unknown corrupt profile fixture: {kind}")
            connection.execute(update(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            ).values(criteria=criteria))

    def _restore_profile(self, profile):
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            )).mappings().one()
            criteria = deepcopy(row["criteria"])
            criteria["role_scoring_profile"] = deepcopy(profile)
            connection.execute(update(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            ).values(criteria=criteria))

    def test_invalid_selected_profile_fails_without_match_or_eligibility_writes_then_recovers(self):
        created = self.client.put(self._profile_url(), json={
            "expected_version": None, "profile": self._definition(),
        })
        self.assertEqual(created.status_code, 200, created.text)
        valid_profile = created.json()["profile"]

        start = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        first_operation_id, first_result = self._schedule_and_run(start)
        self.assertEqual(first_result["status"], "COMPLETED", first_result)
        initial_matches = self._matches()
        self.assertEqual(len(initial_matches), 1)
        self.assertEqual(initial_matches[0]["job_id"], "systems-job")
        self.assertEqual(initial_matches[0]["score"], 20.0)
        initial_explanation = self.client.get(self._explanation_url())
        self.assertEqual(initial_explanation.status_code, 200, initial_explanation.text)
        self.assertEqual(initial_explanation.json()["scoring_profile"]["version"],
                         valid_profile["version"])
        initial_eligibility = self._hard_eligibility()
        self.assertEqual(initial_eligibility, {
            "systems-job": True, "hard-excluded-job": False,
        })
        visible_before = self._visible_search_job_ids()
        self.assertEqual(visible_before, {"systems-job"})

        cases = (
            ("unsupported_schema", 50, "systems-job:ai-v2"),
            ("negative_weight", 80, "systems-job:ai-v3"),
        )
        last_at = start
        for kind, next_signal_score, next_evidence_ref in cases:
            with self.subTest(corruption=kind):
                before_bad_operation = self._matches()
                hard_before = self._hard_eligibility()
                visible_hard_before = self._visible_search_job_ids()
                self._set_ai_signal(next_signal_score, next_evidence_ref)
                self._corrupt_profile(kind)

                failed_at = last_at + timedelta(seconds=301)
                operation_id, result = self._schedule_and_run(failed_at)
                self.assertNotEqual(operation_id, first_operation_id)
                self.assertEqual(result["status"], "DEAD_LETTERED", result)
                with self.store.engine.connect() as connection:
                    operation = dict(connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id,
                    )).mappings().one())
                self.assertEqual(operation["state"], "DEAD_LETTERED")
                self.assertTrue(operation["error"])
                self.assertEqual(self._matches(), before_bad_operation)
                self.assertEqual(self._hard_eligibility(), hard_before)
                self.assertEqual(hard_before, initial_eligibility)
                self.assertEqual(self._visible_search_job_ids(), visible_hard_before)
                self.assertEqual(visible_hard_before, visible_before)

                # Repair the persisted criteria fixture, then retry after its scheduled cadence.
                self._restore_profile(valid_profile)
                repaired = self.client.get(self._profile_url())
                self.assertEqual(repaired.status_code, 200, repaired.text)
                self.assertEqual(repaired.json()["profile"], valid_profile)
                retry_at = failed_at + timedelta(seconds=301)
                retry_id, retry_result = self._schedule_and_run(retry_at)
                self.assertNotEqual(retry_id, operation_id)
                self.assertEqual(retry_result["status"], "COMPLETED", retry_result)

                recovered_matches = self._matches()
                self.assertEqual(len(recovered_matches), 1)
                recovered = recovered_matches[0]
                self.assertEqual(recovered["job_id"], "systems-job")
                self.assertEqual(recovered["score"], float(next_signal_score))
                self.assertEqual(recovered["components"]["scoring_profile"]["profile_version"],
                                 valid_profile["version"])
                explanation = self.client.get(self._explanation_url())
                self.assertEqual(explanation.status_code, 200, explanation.text)
                explained = explanation.json()
                self.assertEqual(explained["score"], float(next_signal_score))
                self.assertEqual(explained["scoring_profile"]["id"], valid_profile["id"])
                self.assertEqual(explained["scoring_profile"]["version"], valid_profile["version"])
                contributions = {row["dimension"]: row
                                 for row in explained["scoring_contributions"]}
                self.assertEqual(contributions["ai_intensity"]["raw_score"],
                                 float(next_signal_score))
                self.assertEqual(contributions["ai_intensity"]["provenance"]["evidence_ref"],
                                 next_evidence_ref)
                self.assertIsNone(contributions["cloud_experience"]["raw_score"])
                self.assertEqual(self._hard_eligibility(), initial_eligibility)
                self.assertEqual(self._visible_search_job_ids(), visible_before)
                last_at = retry_at


if __name__ == "__main__":
    unittest.main()
