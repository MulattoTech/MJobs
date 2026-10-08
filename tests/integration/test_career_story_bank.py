"""Connected API acceptance for profile-scoped, versioned career stories."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class CareerStoryBankApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-career-story-api-")
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'stories.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.test"})
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            operation_mode="PERSONAL"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    @staticmethod
    def story_payload(**overrides):
        payload = {
            "kind": "STAR_STORY",
            "title": "Reduced report latency",
            "text": "Situation: reports were slow. Task: improve them. "
                    "Action: optimized Python queries. Result: reporting ran faster.",
            "fact_ids": ["python"],
            "tags": ["performance", "reporting"],
            "allowed_uses": ["RESUME", "COVER_LETTER", "INTERVIEW_PREPARATION"],
        }
        return {**payload, **overrides}

    def test_create_review_revise_and_read_back_only_current_review_state(self):
        created = self.client.post(
            "/profiles/fixture/career-stories", json=self.story_payload())
        self.assertEqual(created.status_code, 201, created.text)
        story = created.json()
        self.assertEqual(story["profile_id"], "fixture")
        self.assertEqual(story["revision"], 1)
        self.assertEqual(story["review_status"], "PENDING")
        self.assertFalse(story["reusable"])
        story_id = story["story_id"]

        listed = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([row["story_id"] for row in listed.json()["stories"]], [story_id])
        self.assertEqual(listed.json()["stories"][0]["review_status"], "PENDING")

        approved = self.client.post(
            f"/profiles/fixture/career-stories/{story_id}/review",
            json={"approved": True, "reviewer_note": "Checked against approved facts.",
                  "expected_revision": 1},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["review_status"], "APPROVED")
        self.assertTrue(approved.json()["reusable"])

        revised = self.client.post(
            f"/profiles/fixture/career-stories/{story_id}/versions",
            json={"expected_revision": 1, **self.story_payload(
                text="Situation: reports were slow. Task: improve them. "
                     "Action: optimized Python queries. Result: response time improved.")},
        )
        self.assertEqual(revised.status_code, 201, revised.text)
        self.assertEqual(revised.json()["revision"], 2)
        self.assertEqual(revised.json()["review_status"], "PENDING")
        self.assertFalse(revised.json()["reusable"])

        readback = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(readback.status_code, 200, readback.text)
        revisions = readback.json()["stories"]
        self.assertEqual(len(revisions), 2)
        latest = next(row for row in revisions if row["is_latest"])
        self.assertEqual(latest["revision"], 2)
        self.assertEqual(latest["review_status"], "PENDING")
        self.assertFalse(latest["reusable"])

    def test_story_identifiers_are_profile_scoped_and_missing_profile_is_not_found(self):
        created = self.client.post(
            "/profiles/fixture/career-stories", json=self.story_payload())
        self.assertEqual(created.status_code, 201, created.text)
        story_id = created.json()["story_id"]

        cross_profile = self.client.post(
            f"/profiles/other/career-stories/{story_id}/review",
            json={"approved": True, "reviewer_note": "", "expected_revision": 1},
        )
        self.assertEqual(cross_profile.status_code, 404, cross_profile.text)
        self.assertEqual(cross_profile.json()["detail"]["code"], "STORY_NOT_FOUND")

        missing_profile = self.client.get("/profiles/missing/career-stories")
        self.assertEqual(missing_profile.status_code, 404, missing_profile.text)
        self.assertEqual(missing_profile.json()["detail"]["code"], "PROFILE_NOT_FOUND")

    def test_stale_review_and_unapproved_facts_are_rejected_without_reusing_story(self):
        created = self.client.post(
            "/profiles/fixture/career-stories", json=self.story_payload())
        self.assertEqual(created.status_code, 201, created.text)
        story_id = created.json()["story_id"]

        stale_review = self.client.post(
            f"/profiles/fixture/career-stories/{story_id}/review",
            json={"approved": True, "reviewer_note": "", "expected_revision": 0},
        )
        self.assertEqual(stale_review.status_code, 409, stale_review.text)
        self.assertEqual(stale_review.json()["detail"]["code"], "REVISION_CONFLICT")

        invalid_facts = self.client.post(
            "/profiles/fixture/career-stories",
            json=self.story_payload(fact_ids=["not-an-approved-fact"]),
        )
        self.assertEqual(invalid_facts.status_code, 422, invalid_facts.text)
        self.assertEqual(invalid_facts.json()["detail"]["code"], "FACTS_UNAVAILABLE")

        still_pending = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(still_pending.status_code, 200, still_pending.text)
        self.assertEqual(len(still_pending.json()["stories"]), 1)
        self.assertEqual(still_pending.json()["stories"][0]["review_status"], "PENDING")
        self.assertFalse(still_pending.json()["stories"][0]["reusable"])


if __name__ == "__main__":
    unittest.main()
