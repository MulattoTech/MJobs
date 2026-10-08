"""Profile-scoped API contract for deterministic skill-gap heatmaps."""
from importlib import import_module
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class SkillGapHeatmapApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-skill-gap-api-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'heatmap.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.test"})

        self.client = None
        try:
            module = import_module("apps.api.skill_gap_heatmap")
            app = FastAPI()
            app.include_router(module.create_skill_gap_heatmap_router(self.store))
            self.client = TestClient(app)
            self.addCleanup(self.client.close)
        except ModuleNotFoundError:
            self.fail("The skill-gap heatmap API router is not available")

    def _seed_match(self, profile_id, suffix, requirement):
        job_id = f"heatmap-job-{suffix}"
        company_id = f"heatmap-company-{suffix}"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name=f"Synthetic Employer {suffix}", domain="example.test",
            ))
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id=company_id, requisition=suffix,
                title="Synthetic systems role", description="Synthetic posting",
                skills=[requirement["skill"]], location="Remote", remote_type="REMOTE",
                attributes={},
            ))
            connection.execute(m.matches.insert().values(
                id=f"heatmap-match-{suffix}", job_id=job_id, profile_id=profile_id,
                profile_version=self.store.profile(profile_id)["version"], score=42.0,
                missing=[requirement["skill"]],
                components={"requirements": [requirement]},
            ))

    def test_heatmap_is_profile_scoped_and_unknown_mentions_are_not_candidate_gaps(self):
        self._seed_match("fixture", "own", {
            "skill": "Python", "strength": "REQUIRED",
            "profile_evidence": "NO_APPROVED_EVIDENCE",
        })
        self._seed_match("fixture", "unknown", {
            "skill": "Rust", "strength": "UNKNOWN",
            "profile_evidence": "NO_APPROVED_EVIDENCE",
        })
        self._seed_match("other", "private", {
            "skill": "Go", "strength": "PREFERRED",
            "profile_evidence": "NO_APPROVED_EVIDENCE",
        })

        response = self.client.get("/profiles/fixture/skill-gap-heatmap")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["profile_id"], "fixture")
        self.assertEqual(payload["jobs_examined"], 2)
        self.assertEqual([row["skill"] for row in payload["skills"]], ["Python", "Rust"])
        self.assertEqual(payload["skills"][0]["no_approved_evidence_job_count"], 1)
        self.assertEqual(payload["skills"][1]["unknown_posting_metadata_job_count"], 1)
        self.assertEqual(payload["skills"][1]["no_approved_evidence_job_count"], 0)
        self.assertNotIn("Go", response.text)

    def test_empty_heatmap_is_a_successful_profile_scoped_read(self):
        response = self.client.get("/profiles/fixture/skill-gap-heatmap")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "profile_id": "fixture",
            "profile_version": self.store.profile("fixture")["version"],
            "jobs_examined": 0,
            "skills": [],
        })

    def test_stale_profile_version_is_rejected_without_returning_heatmap_data(self):
        self._seed_match("fixture", "stale", {
            "skill": "Python", "strength": "REQUIRED",
            "profile_evidence": "NO_APPROVED_EVIDENCE",
        })

        response = self.client.get(
            "/profiles/fixture/skill-gap-heatmap",
            params={"profile_version": "0" * 64},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "SKILL_GAP_PROFILE_VERSION_STALE")
        self.assertNotIn("Python", response.text)


if __name__ == "__main__":
    unittest.main()
