"""Profile isolation and read-only API guarantees for skill-gap heatmaps."""
from importlib import import_module
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class SkillGapHeatmapProfileIsolationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="mjobs-skill-gap-profile-isolation-"
        )
        self.addCleanup(self.directory.cleanup)
        database = Path(self.directory.name) / "heatmap.db"
        self.store = Store(f"sqlite:///{database.as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({
            **PROFILE,
            "id": "other",
            "name": "Other Profile",
            "email": "other@example.test",
        })

        module = import_module("apps.api.skill_gap_heatmap")
        app = FastAPI()
        app.include_router(module.create_skill_gap_heatmap_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_match(self, profile_id, suffix, skill):
        job_id = f"profile-isolation-job-{suffix}"
        company_id = f"profile-isolation-company-{suffix}"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name=f"Synthetic Employer {suffix}",
                domain="example.test",
            ))
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id=company_id, requisition=suffix,
                title="Synthetic systems role", description="Synthetic posting",
                skills=[skill], location="Remote", remote_type="REMOTE",
                attributes={},
            ))
            connection.execute(m.matches.insert().values(
                id=f"profile-isolation-match-{suffix}", job_id=job_id,
                profile_id=profile_id,
                profile_version=self.store.profile(profile_id)["version"],
                score=42.0, missing=[skill],
                components={"requirements": [{
                    "skill": skill,
                    "strength": "REQUIRED",
                    "profile_evidence": "NO_APPROVED_EVIDENCE",
                }]},
            ))

    def _read_state(self):
        tables = (m.profiles, m.matches, m.applications, m.audit)
        state = {}
        with self.store.engine.connect() as connection:
            for table in tables:
                rows = connection.execute(select(table)).mappings()
                state[table.name] = sorted(
                    tuple((column, repr(value)) for column, value in row.items())
                    for row in rows
                )
        return state

    def test_each_profile_receives_only_its_current_match_evidence(self):
        self._seed_match("fixture", "fixture-python", "Python")
        self._seed_match("other", "other-go", "Go")

        fixture_response = self.client.get(
            "/profiles/fixture/skill-gap-heatmap"
        )
        other_response = self.client.get("/profiles/other/skill-gap-heatmap")

        self.assertEqual(fixture_response.status_code, 200, fixture_response.text)
        self.assertEqual(other_response.status_code, 200, other_response.text)
        fixture = fixture_response.json()
        other = other_response.json()
        self.assertEqual(fixture["profile_id"], "fixture")
        self.assertEqual(fixture["jobs_examined"], 1)
        self.assertEqual([row["skill"] for row in fixture["skills"]], ["Python"])
        self.assertEqual(other["profile_id"], "other")
        self.assertEqual(other["jobs_examined"], 1)
        self.assertEqual([row["skill"] for row in other["skills"]], ["Go"])
        self.assertNotIn("Go", fixture_response.text)
        self.assertNotIn("Python", other_response.text)

    def test_stale_profile_version_rejection_does_not_write_any_profile_state(self):
        self._seed_match("fixture", "stale-python", "Python")
        before = self._read_state()

        response = self.client.get(
            "/profiles/fixture/skill-gap-heatmap",
            params={"profile_version": "0" * 64},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "SKILL_GAP_PROFILE_VERSION_STALE",
        )
        self.assertNotIn("Python", response.text)
        self.assertEqual(self._read_state(), before)


if __name__ == "__main__":
    unittest.main()
