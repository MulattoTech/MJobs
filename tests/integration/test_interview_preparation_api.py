"""Connected API acceptance for durable, profile/job-scoped prep packs."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.interview_preparation import create_interview_preparation_router
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.career_profile.story_bank import create_story, review_story
from tests.integration.test_spine import PROFILE


class InterviewPreparationApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-interview-prep-api-")
        self.root = Path(self.directory.name)
        self.database = (self.root / "interview-prep.sqlite").as_posix()
        self.store = Store(f"sqlite:///{self.database}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = deepcopy(PROFILE)
        profile["facts"] = [
            {
                "id": "prep-fact-python",
                "text": "Maintained synthetic Python support services for four test teams.",
                "kind": "accomplishment",
                "source": "Synthetic fixture work history",
                "verified": True,
                "user_approved": True,
                "confidence": 100,
            },
            {
                "id": "prep-fact-missing",
                "text": "Unreviewed synthetic Kubernetes experience.",
                "kind": "accomplishment",
                "source": "Unreviewed synthetic note",
                "verified": False,
                "user_approved": False,
                "confidence": 20,
            },
        ]
        self.store.save_profile(profile)
        other = deepcopy(profile)
        other.update({"id": "other", "name": "Other Synthetic Profile"})
        self.store.save_profile(other)
        self.job_id = "interview-prep-job"
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "interview-prep-company", "name": "Synthetic Systems",
                "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": self.job_id, "company_id": "interview-prep-company",
                "requisition": "interview-prep-job", "title": "Python Support Engineer",
                "description": "Synthetic job requiring Python incident response and support.",
                "skills": ["Python", "incident response"], "location": "Remote",
                "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "interview-prep-route", "job_id": self.job_id,
                "company_id": "interview-prep-company", "provider": "fixture",
                "tenant": "interview-prep", "requisition": "interview-prep-job",
                "url": "https://jobs.example.test/interview-prep",
            })
        app = FastAPI()
        app.include_router(create_interview_preparation_router(self.store))
        self.client = TestClient(app)
        self.story_one = self._approved_story("Synthetic Python support recovery")
        self.story_two = self._approved_story("Synthetic incident triage")
        self.collection = f"/profiles/fixture/jobs/{self.job_id}/interview-prep-packs"

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _approved_story(self, title, *, allowed_uses=None):
        story = create_story(self.store, "fixture", {
            "kind": "STAR_STORY",
            "title": title,
            "text": f"User-authored synthetic account for {title}.",
            "fact_ids": ["prep-fact-python"],
            "tags": ["python", "support"],
            "allowed_uses": allowed_uses or ["INTERVIEW_PREPARATION"],
        })
        return review_story(
            self.store, "fixture", story["story_id"], True, None,
            expected_revision=story["revision"],
        )

    def _create(self, story_ids):
        return self.client.post(self.collection, json={
            "selected_story_revision_ids": story_ids,
        })

    def test_pack_is_idempotent_versioned_profile_scoped_and_readable_after_reopen(self):
        first = self._create([self.story_one["story_id"]])
        self.assertEqual(first.status_code, 200, first.text)
        pack_one = first.json()
        self.assertEqual(pack_one["schema_version"], 1)
        self.assertEqual(pack_one["profile_id"], "fixture")
        self.assertEqual(pack_one["job_id"], self.job_id)
        self.assertEqual(pack_one["pack_version"], 1)
        self.assertEqual(pack_one["selected_story_revision_ids"], [self.story_one["story_id"]])
        self.assertIsInstance(pack_one["questions"], list)
        self.assertIsInstance(pack_one["talking_points"], list)
        self.assertIsInstance(pack_one["star_prompts"], list)
        self.assertIsInstance(pack_one["missing_evidence"], list)
        for entry in pack_one["questions"] + pack_one["talking_points"] + pack_one["star_prompts"]:
            self.assertTrue(entry.get("source_refs"), entry)
        self.assertIn("export_text", pack_one)
        self.assertTrue(pack_one["export_text"].strip())
        self.assertIn("missing_evidence", pack_one)
        self.assertNotIn("generated_answer", pack_one)

        repeated = self._create([self.story_one["story_id"]])
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json(), pack_one)
        self.assertEqual(repeated.json()["pack_id"], pack_one["pack_id"])
        self.assertEqual(repeated.json()["pack_version"], 1)
        self.assertEqual(repeated.json()["export_text"], pack_one["export_text"])

        changed = self._create([
            self.story_one["story_id"], self.story_two["story_id"],
        ])
        self.assertEqual(changed.status_code, 200, changed.text)
        pack_two = changed.json()
        self.assertEqual(pack_two["pack_version"], 2)
        self.assertNotEqual(pack_two["pack_id"], pack_one["pack_id"])

        listed = self.client.get(self.collection)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["profile_id"], "fixture")
        self.assertEqual(listed.json()["job_id"], self.job_id)
        self.assertEqual([row["pack_version"] for row in listed.json()["packs"]], [1, 2])

        with self.store.engine.connect() as connection:
            before = [dict(row) for row in connection.execute(
                select(m.interview_prep_packs).order_by(m.interview_prep_packs.c.pack_version)
            ).mappings()]

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database}")
        self.store.migrate()
        reloaded_app = FastAPI()
        reloaded_app.include_router(create_interview_preparation_router(self.store))
        self.client = TestClient(reloaded_app)
        detail = self.client.get(f"{self.collection}/{pack_two['pack_id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json(), pack_two)
        with self.store.engine.connect() as connection:
            after = [dict(row) for row in connection.execute(
                select(m.interview_prep_packs).order_by(m.interview_prep_packs.c.pack_version)
            ).mappings()]
        self.assertEqual(after, before)

        other_profile = self.client.get(
            f"/profiles/other/jobs/{self.job_id}/interview-prep-packs",
        )
        self.assertEqual(other_profile.status_code, 200, other_profile.text)
        self.assertEqual(other_profile.json()["packs"], [])
        foreign_detail = self.client.get(
            f"/profiles/other/jobs/{self.job_id}/interview-prep-packs/{pack_two['pack_id']}",
        )
        self.assertEqual(foreign_detail.status_code, 404, foreign_detail.text)

    def test_rejects_noncurrent_or_wrong_use_revision_without_persisting(self):
        wrong_use = self._approved_story(
            "Synthetic resume-only story", allowed_uses=["RESUME"],
        )
        invalid = self._create([wrong_use["story_id"]])
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertIn("code", invalid.json()["detail"])
        duplicate = self._create([self.story_one["story_id"], self.story_one["story_id"]])
        self.assertEqual(duplicate.status_code, 422, duplicate.text)
        malformed = self.client.post(self.collection, json={"selected_story_ids": []})
        self.assertEqual(malformed.status_code, 422, malformed.text)
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.interview_prep_packs)).all()
        self.assertEqual(rows, [])

    def test_missing_profile_or_job_is_not_created_as_a_pack(self):
        missing_profile = self.client.get(
            f"/profiles/missing/jobs/{self.job_id}/interview-prep-packs",
        )
        self.assertEqual(missing_profile.status_code, 404, missing_profile.text)
        missing_job = self.client.post(
            "/profiles/fixture/jobs/missing/interview-prep-packs",
            json={"selected_story_revision_ids": [self.story_one["story_id"]]},
        )
        self.assertEqual(missing_job.status_code, 404, missing_job.text)
        missing_job_list = self.client.get(
            "/profiles/fixture/jobs/missing/interview-prep-packs",
        )
        self.assertEqual(missing_job_list.status_code, 404, missing_job_list.text)
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.interview_prep_packs)).all()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
