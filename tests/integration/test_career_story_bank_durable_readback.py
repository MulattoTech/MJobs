"""SQLite reopen acceptance for immutable, reviewed career-story revisions."""
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.career_story_bank import create_career_story_bank_router
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class CareerStoryBankDurableReadbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-story-bank-durable-")
        self.database_path = Path(self.directory.name) / "stories.sqlite"
        self.database_url = f"sqlite:///{self.database_path.as_posix()}"
        self.store = None
        self.client = None
        self._open_store_and_client(seed_profile=True)

    def tearDown(self):
        self._close_store_and_client()
        self.directory.cleanup()

    def _open_store_and_client(self, seed_profile=False):
        self.store = Store(self.database_url)
        self.store.migrate()
        if seed_profile:
            self.store.save_profile(PROFILE)
        app = FastAPI()
        app.include_router(create_career_story_bank_router(self.store))
        self.client = TestClient(app)

    def _close_store_and_client(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.store is not None:
            self.store.engine.dispose()
            self.store = None

    @staticmethod
    def story_payload(**overrides):
        return {
            "kind": "STAR_STORY",
            "title": "Reduced report latency",
            "text": "Situation: reports were slow. Task: improve them. "
                    "Action: optimized Python queries. Result: reporting ran faster.",
            "fact_ids": ["python"],
            "tags": ["performance", "reporting"],
            "allowed_uses": ["RESUME", "INTERVIEW_PREPARATION"],
            **overrides,
        }

    def test_reviewed_revision_and_append_only_edit_survive_sqlite_reopen(self):
        created_response = self.client.post(
            "/profiles/fixture/career-stories", json=self.story_payload())
        self.assertEqual(created_response.status_code, 201, created_response.text)
        first = created_response.json()

        approved_response = self.client.post(
            f"/profiles/fixture/career-stories/{first['story_id']}/review",
            json={"approved": True, "reviewer_note": "Checked against approved facts.",
                  "expected_revision": 1},
        )
        self.assertEqual(approved_response.status_code, 200, approved_response.text)
        approved = approved_response.json()
        self.assertTrue(approved["reusable"])
        original_created_at = approved["created_at"]
        original_reviewed_at = approved["reviewed_at"]

        # Closing every client/engine catches accidental reliance on process memory.
        self._close_store_and_client()
        self._open_store_and_client()

        readback_response = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(readback_response.status_code, 200, readback_response.text)
        [persisted_first] = readback_response.json()["stories"]
        self.assertEqual(persisted_first["story_id"], first["story_id"])
        self.assertEqual(persisted_first["story_key"], first["story_key"])
        self.assertEqual(persisted_first["revision"], 1)
        self.assertEqual(persisted_first["title"], first["title"])
        self.assertEqual(persisted_first["text"], first["text"])
        self.assertEqual(persisted_first["fact_ids"], ["python"])
        self.assertEqual(persisted_first["fact_fingerprints"], first["fact_fingerprints"])
        self.assertEqual(persisted_first["review_status"], "APPROVED")
        self.assertEqual(persisted_first["review_note"], "Checked against approved facts.")
        self.assertEqual(persisted_first["created_at"], original_created_at)
        self.assertEqual(persisted_first["reviewed_at"], original_reviewed_at)
        self.assertTrue(persisted_first["reusable"])

        revised_response = self.client.post(
            f"/profiles/fixture/career-stories/{first['story_id']}/versions",
            json={"expected_revision": 1, **self.story_payload(
                title="Further improved report latency",
                text="The user-authored account now includes the measured follow-up result.")},
        )
        self.assertEqual(revised_response.status_code, 201, revised_response.text)
        second = revised_response.json()
        self.assertEqual(second["story_key"], first["story_key"])
        self.assertEqual(second["revision"], 2)
        self.assertNotEqual(second["story_id"], first["story_id"])
        self.assertEqual(second["review_status"], "PENDING")
        self.assertFalse(second["reusable"])

        self._close_store_and_client()
        self._open_store_and_client()

        durable_response = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(durable_response.status_code, 200, durable_response.text)
        revisions = durable_response.json()["stories"]
        self.assertEqual(len(revisions), 2)
        persisted_second = next(row for row in revisions if row["revision"] == 2)
        persisted_first = next(row for row in revisions if row["revision"] == 1)

        # Editing appends a new immutable revision and retains prior reviewed bytes.
        self.assertEqual(persisted_first["story_id"], first["story_id"])
        self.assertEqual(persisted_first["title"], first["title"])
        self.assertEqual(persisted_first["text"], first["text"])
        self.assertEqual(persisted_first["fact_ids"], ["python"])
        self.assertEqual(persisted_first["fact_fingerprints"], first["fact_fingerprints"])
        self.assertEqual(persisted_first["review_status"], "APPROVED")
        self.assertEqual(persisted_first["review_note"], "Checked against approved facts.")
        self.assertEqual(persisted_first["created_at"], original_created_at)
        self.assertEqual(persisted_first["reviewed_at"], original_reviewed_at)
        self.assertFalse(persisted_first["is_latest"])
        self.assertFalse(persisted_first["reusable"])

        self.assertEqual(persisted_second["story_id"], second["story_id"])
        self.assertEqual(persisted_second["title"], "Further improved report latency")
        self.assertEqual(persisted_second["text"],
                         "The user-authored account now includes the measured follow-up result.")
        self.assertEqual(persisted_second["review_status"], "PENDING")
        self.assertTrue(persisted_second["is_latest"])
        self.assertFalse(persisted_second["reusable"])


if __name__ == "__main__":
    unittest.main()
