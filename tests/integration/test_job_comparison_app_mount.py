"""Connected app-mount contract for saved-match job comparison."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE


class JobComparisonAppMountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-job-comparison-mount-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'comparison.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({
            **deepcopy(PROFILE), "id": "other", "email": "other@example.test",
        })
        self._save_search("selected-search", "fixture")
        self._save_search("other-search", "other")
        self._seed_jobs_and_matches()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            operation_mode="PERSONAL",
        ))
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, profile_id):
        return save_search_profile(self.store, {
            "id": search_id,
            "profile_id": profile_id,
            "name": search_id,
            "enabled": False,
            "include_titles": [],
            "exclude_titles": [],
            "required_keywords": [],
            "optional_keywords": [],
            "excluded_keywords": [],
            "exact_phrases": [],
            "technology_aliases": {},
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [],
        })

    def _seed_jobs_and_matches(self):
        current_version = self.store.profile("fixture")["version"]
        other_version = self.store.profile("other")["version"]
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="comparison-company", name="Comparison Fixture Co.",
                domain="comparison.example.test",
            ))
            for job_id, title, required in (
                ("job-one", "Systems Administrator", ["ACTIVE_DIRECTORY", "POWERSHELL"]),
                ("job-two", "Infrastructure Engineer", ["ACTIVE_DIRECTORY", "LINUX"]),
                ("job-foreign", "Foreign Profile Role", ["LINUX"]),
                ("job-stale", "Stale Match Role", ["LINUX"]),
            ):
                connection.execute(m.jobs.insert().values(
                    id=job_id,
                    company_id="comparison-company",
                    requisition=job_id,
                    title=title,
                    description="Synthetic fixture; prose is not comparison evidence.",
                    skills=[],
                    location="Unknown",
                    remote_type="UNKNOWN",
                    attributes={
                        "skill_evidence": "structured_requirements",
                        "required_skills": required,
                        "source_type": "fixture_source",
                        "requirement_source_ref": f"fixture:{job_id}:requirements",
                    },
                ))
            for ordinal, job_id in enumerate(("job-one", "job-two", "job-stale"), start=1):
                score = 70.0 + ordinal
                connection.execute(m.matches.insert().values(
                    id=f"match-{job_id}",
                    job_id=job_id,
                    profile_id="fixture",
                    profile_version=("outdated-profile-version" if job_id == "job-stale"
                                     else current_version),
                    score=score,
                    missing=[],
                    components={"scoring_contributions": [{
                        "dimension": "skills",
                        "weight": 100.0,
                        "raw_score": score,
                        "weighted_contribution": score,
                        "explanation": "Persisted synthetic match contribution.",
                        "provenance": {"source": "fixture_match"},
                    }]},
                ))
            connection.execute(m.matches.insert().values(
                id="match-job-foreign",
                job_id="job-foreign",
                profile_id="other",
                profile_version=other_version,
                score=80.0,
                missing=[],
                components={"scoring_contributions": []},
            ))

    @staticmethod
    def _url(profile_id="fixture", search_profile_id="selected-search"):
        return (f"/profiles/{profile_id}/search-profiles/{search_profile_id}"
                "/jobs/compare")

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(
                    select(table),
                ).mappings()]
                for table in m.metadata.sorted_tables
            }

    def test_create_app_mounts_comparison_route_and_returns_scoped_saved_match_contract(self):
        before = self._snapshot()

        response = self.client.post(self._url(), json={
            "job_ids": ["job-one", "job-two"],
        })

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["search_profile_id"], "selected-search")
        self.assertEqual(body["job_ids"], ["job-one", "job-two"])
        self.assertEqual([job["job_id"] for job in body["jobs"]], ["job-one", "job-two"])
        self.assertEqual([job["match_score"] for job in body["jobs"]], [71.0, 72.0])
        self.assertEqual(body["jobs"][0]["scoring_contributions"][0]["provenance"], {
            "source": "fixture_match",
        })
        self.assertEqual(body["shared_requirements"], [{
            "term": "active directory",
            "job_ids": ["job-one", "job-two"],
            "provenance": [
                {"job_id": "job-one", "source": "fixture_source",
                 "evidence_ref": "fixture:job-one:requirements"},
                {"job_id": "job-two", "source": "fixture_source",
                 "evidence_ref": "fixture:job-two:requirements"},
            ],
        }])
        self.assertEqual(body["job_specific_requirements"], [
            {"term": "powershell", "job_id": "job-one", "provenance": {
                "source": "fixture_source", "evidence_ref": "fixture:job-one:requirements",
            }},
            {"term": "linux", "job_id": "job-two", "provenance": {
                "source": "fixture_source", "evidence_ref": "fixture:job-two:requirements",
            }},
        ])
        self.assertEqual(body["unknowns"], [])
        self.assertEqual(self._snapshot(), before)

    def test_foreign_search_job_and_stale_match_fail_closed_without_writes(self):
        before = self._snapshot()

        foreign_search = self.client.post(self._url(search_profile_id="other-search"), json={
            "job_ids": ["job-one", "job-two"],
        })
        self.assertEqual(foreign_search.status_code, 409, foreign_search.text)
        self.assertEqual(foreign_search.json()["detail"]["code"],
                         "JOB_COMPARISON_SCOPE_MISMATCH")

        for job_id, expected_code in (
            ("job-foreign", "JOB_COMPARISON_SCOPE_MISMATCH"),
            ("job-stale", "JOB_COMPARISON_STALE_CONTEXT"),
        ):
            with self.subTest(job_id=job_id):
                response = self.client.post(self._url(), json={
                    "job_ids": ["job-one", job_id],
                })
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["code"], expected_code)

        foreign_profile = self.client.post(self._url(profile_id="other"), json={
            "job_ids": ["job-one", "job-two"],
        })
        self.assertEqual(foreign_profile.status_code, 409, foreign_profile.text)
        self.assertEqual(foreign_profile.json()["detail"]["code"],
                         "JOB_COMPARISON_SCOPE_MISMATCH")
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
