"""Connected API acceptance for read-only, job-scoped reusable-story selection."""
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.career_story_selection import create_career_story_selection_router
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.career_profile.story_bank import (
    create_story,
    list_stories,
    review_story,
    revise_story,
)
from services.documents.tailor import target_fingerprint
from tests.integration.test_spine import PROFILE


class CareerStorySelectionApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-story-selection-api-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'selection.sqlite').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.test"})
        self.job_id = "story-selection-job"
        self._seed_job()
        app = FastAPI()
        app.include_router(create_career_story_selection_router(self.store))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _seed_job(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "story-selection-company", "name": "Synthetic Systems",
                "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": self.job_id, "company_id": "story-selection-company",
                "requisition": "story-selection-job", "title": "Python Support Engineer",
                "description": "Synthetic saved job for story selection.", "skills": ["Python"],
                "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "story-selection-route", "job_id": self.job_id,
                "company_id": "story-selection-company", "provider": "fixture",
                "tenant": "story-selection", "requisition": "story-selection-job",
                "url": "https://jobs.example.test/story-selection",
            })

    @staticmethod
    def story_payload(title, allowed_uses, text=None):
        return {
            "kind": "STAR_STORY",
            "title": title,
            "text": text or f"User-authored content for {title}.",
            "fact_ids": ["python"],
            "tags": ["support"],
            "allowed_uses": allowed_uses,
        }

    def _create_story(self, profile_id, title, allowed_uses, *, approve=True):
        story = create_story(
            self.store, profile_id,
            self.story_payload(title, allowed_uses),
        )
        if approve:
            story = review_story(
                self.store, profile_id, story["story_id"], True, None,
                expected_revision=story["revision"],
            )
        return story

    def _update_profile(self, profile_id):
        profile = self.store.profile(profile_id)
        updated = {
            **PROFILE,
            "id": profile_id,
            "name": profile["name"] + " Updated",
            "email": profile["email"],
            "facts": [
                {**fact, "text": "Python 3.12"}
                if fact["id"] == "python" else fact
                for fact in PROFILE["facts"]
            ],
        }
        return self.store.save_profile(
            updated, expected_version=profile["version"],
        )

    def test_only_current_approved_reusable_rows_with_requested_use_are_listed_read_only(self):
        stale = self._create_story("fixture", "Stale story", ["RESUME"])
        self._update_profile("fixture")
        current = self._create_story(
            "fixture", "Current resume story", ["RESUME", "COVER_LETTER"])
        other_use = self._create_story("fixture", "Cover letter only", ["COVER_LETTER"])
        pending = self._create_story("fixture", "Unreviewed story", ["RESUME"], approve=False)
        older = self._create_story("fixture", "Revised story", ["RESUME"])
        latest = revise_story(
            self.store, "fixture", older["story_id"],
            self.story_payload("Revised story", ["RESUME"], "New pending version."),
            expected_revision=older["revision"],
        )
        foreign = self._create_story("other", "Other profile story", ["RESUME"])
        stale_now = next(row for row in list_stories(self.store, "fixture")
                         if row["story_id"] == stale["story_id"])

        with self.store.engine.connect() as connection:
            rows_before = [dict(row) for row in connection.execute(
                select(m.career_stories).order_by(m.career_stories.c.id)).mappings()]
            facts_before = [dict(row) for row in connection.execute(
                select(m.facts).order_by(m.facts.c.id)).mappings()]
            audit_before = [dict(row) for row in connection.execute(
                select(m.audit).order_by(m.audit.c.id)).mappings()]

        response = self.client.get(
            f"/profiles/fixture/jobs/{self.job_id}/career-stories/selectable",
            params={"allowed_use": "RESUME"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["job_id"], self.job_id)
        self.assertEqual(body["allowed_use"], "RESUME")
        self.assertEqual(body["profile_version"], self.store.profile("fixture")["version"])
        job = self.store.job(self.job_id, "fixture")
        self.assertEqual(body["job_fingerprint"], target_fingerprint(job))
        candidates = body["stories"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["story_id"], current["story_id"])
        self.assertEqual(candidates[0]["story_key"], current["story_key"])
        self.assertEqual(candidates[0]["revision"], 1)
        self.assertEqual(candidates[0]["text"], current["text"])
        self.assertEqual(candidates[0]["review_status"], "APPROVED")
        self.assertTrue(candidates[0]["reusable"])
        self.assertEqual(candidates[0]["provenance"], {
            "fact_ids": ["python"],
            "fact_fingerprints": current["fact_fingerprints"],
        })
        self.assertNotIn("generated_text", candidates[0])
        self.assertNotIn("generation", candidates[0])
        with self.store.engine.connect() as connection:
            self.assertEqual([dict(row) for row in connection.execute(
                select(m.career_stories).order_by(m.career_stories.c.id)).mappings()], rows_before)
            self.assertEqual([dict(row) for row in connection.execute(
                select(m.facts).order_by(m.facts.c.id)).mappings()], facts_before)
            self.assertEqual([dict(row) for row in connection.execute(
                select(m.audit).order_by(m.audit.c.id)).mappings()], audit_before)

        cover_letter = self.client.get(
            f"/profiles/fixture/jobs/{self.job_id}/career-stories/selectable",
            params={"allowed_use": "COVER_LETTER"},
        )
        self.assertEqual(cover_letter.status_code, 200, cover_letter.text)
        self.assertEqual([row["story_id"] for row in cover_letter.json()["stories"]],
                         sorted([current["story_id"], other_use["story_id"]]))

        # Keep the named negative cases observable in the fixture's actual source rows.
        self.assertFalse(stale_now["reusable"])
        self.assertEqual(other_use["review_status"], "APPROVED")
        self.assertFalse(pending["reusable"])
        self.assertFalse(latest["reusable"])
        self.assertTrue(foreign["reusable"])

    def test_profile_job_and_allowed_use_are_validated_before_selection(self):
        self._create_story("fixture", "Current resume story", ["RESUME"])
        base = "/profiles/fixture/jobs"

        invalid_use = self.client.get(
            f"{base}/{self.job_id}/career-stories/selectable",
            params={"allowed_use": "APPLICATION_ANSWER"},
        )
        self.assertEqual(invalid_use.status_code, 422, invalid_use.text)
        self.assertEqual(invalid_use.json(), {
            "detail": {"code": "CAREER_STORY_SELECTION_USE_INVALID"},
        })

        missing_use = self.client.get(
            f"{base}/{self.job_id}/career-stories/selectable",
        )
        self.assertEqual(missing_use.status_code, 422, missing_use.text)
        self.assertEqual(missing_use.json(), {
            "detail": {"code": "CAREER_STORY_SELECTION_USE_INVALID"},
        })

        missing_profile = self.client.get(
            f"/profiles/missing/jobs/{self.job_id}/career-stories/selectable",
            params={"allowed_use": "RESUME"},
        )
        self.assertEqual(missing_profile.status_code, 404, missing_profile.text)
        self.assertEqual(missing_profile.json(), {
            "detail": {"code": "CAREER_STORY_SELECTION_PROFILE_NOT_FOUND"},
        })

        missing_job = self.client.get(
            "/profiles/fixture/jobs/missing/career-stories/selectable",
            params={"allowed_use": "RESUME"},
        )
        self.assertEqual(missing_job.status_code, 404, missing_job.text)
        self.assertEqual(missing_job.json(), {
            "detail": {"code": "CAREER_STORY_SELECTION_JOB_NOT_FOUND"},
        })

    def test_listing_is_profile_scoped_even_when_another_profile_has_approved_candidates(self):
        self._create_story("other", "Other profile story", ["RESUME"])
        response = self.client.get(
            f"/profiles/fixture/jobs/{self.job_id}/career-stories/selectable",
            params={"allowed_use": "RESUME"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["stories"], [])
        self.assertEqual(response.json()["profile_id"], "fixture")


if __name__ == "__main__":
    unittest.main()
