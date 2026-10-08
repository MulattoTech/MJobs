"""API conflict handling for invalid and stale career-story review revisions."""
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.career_story_bank import create_career_story_bank_router
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class CareerStoryBankRevisionConflictTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-story-revision-conflict-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'stories.sqlite').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(dict(PROFILE))
        app = FastAPI()
        app.include_router(create_career_story_bank_router(self.store))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    @staticmethod
    def story_payload():
        return {
            "kind": "STAR_STORY",
            "title": "Private latency improvement story",
            "text": "Private details: optimized Python report queries and improved latency.",
            "fact_ids": ["python"],
            "tags": ["performance"],
            "allowed_uses": ["RESUME", "INTERVIEW_PREPARATION"],
        }

    def test_nonpositive_review_revision_is_sanitized_conflict_without_review_write(self):
        created = self.client.post(
            "/profiles/fixture/career-stories", json=self.story_payload())
        self.assertEqual(created.status_code, 201, created.text)
        story = created.json()

        for expected_revision in (0, -1):
            with self.subTest(expected_revision=expected_revision):
                response = self.client.post(
                    f"/profiles/fixture/career-stories/{story['story_id']}/review",
                    json={"approved": True,
                          "reviewer_note": "Private review note",
                          "expected_revision": expected_revision},
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json(), {
                    "detail": {"code": "REVISION_CONFLICT"},
                })
                for private_value in (
                    story["title"], story["text"], "Private review note"):
                    self.assertNotIn(private_value, response.text)

        readback = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(len(readback.json()["stories"]), 1)
        unchanged = readback.json()["stories"][0]
        self.assertEqual(unchanged["story_id"], story["story_id"])
        self.assertEqual(unchanged["revision"], 1)
        self.assertEqual(unchanged["review_status"], "PENDING")
        self.assertFalse(unchanged["reusable"])
        self.assertEqual(unchanged["review_note"], None)
        self.assertEqual(unchanged["title"], story["title"])
        self.assertEqual(unchanged["text"], story["text"])

    def test_malformed_review_requests_keep_validation_errors(self):
        created = self.client.post(
            "/profiles/fixture/career-stories", json=self.story_payload())
        self.assertEqual(created.status_code, 201, created.text)
        story_id = created.json()["story_id"]
        path = f"/profiles/fixture/career-stories/{story_id}/review"

        malformed_requests = (
            ({"approved": True, "reviewer_note": "missing revision"}, "INVALID_STORY"),
            ({"approved": True, "reviewer_note": "string revision",
              "expected_revision": "1"}, "INVALID_STORY"),
            ({"approved": True, "reviewer_note": "boolean revision",
              "expected_revision": True}, "INVALID_STORY"),
            ({"approved": "yes", "reviewer_note": "malformed approval",
              "expected_revision": 0}, "INVALID_STORY"),
            ({"approved": True, "reviewer_note": "x" * 2001,
              "expected_revision": 0}, "INVALID_STORY"),
            (["not", "an", "object"], "INVALID_REVIEW"),
        )
        for payload, code in malformed_requests:
            with self.subTest(payload=payload):
                response = self.client.post(path, json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json(), {"detail": {"code": code}})

        readback = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(readback.status_code, 200, readback.text)
        [unchanged] = readback.json()["stories"]
        self.assertEqual(unchanged["review_status"], "PENDING")
        self.assertFalse(unchanged["reusable"])


if __name__ == "__main__":
    unittest.main()
