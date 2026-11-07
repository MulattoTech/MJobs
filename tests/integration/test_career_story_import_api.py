"""HTTP import boundary for strict, bounded career-story JSON."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from services.career_profile.story_selection import StorySelectionError, select_stories_for_job
from tests.integration.test_spine import PROFILE


def story(**changes):
    return {
        "kind": "STAR_STORY",
        "title": "Improved reports",
        "text": "Situation: reports were slow. Action: tuned queries. Result: faster reports.",
        "fact_ids": ["python"],
        "tags": ["reporting"],
        "allowed_uses": ["RESUME", "INTERVIEW_PREPARATION"],
        **changes,
    }


class CareerStoryImportApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-story-import-")
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'stories.sqlite').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(dict(PROFILE))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root, operation_mode="PERSONAL"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _post(self, stories):
        return self.client.post(
            "/profiles/fixture/career-stories/import",
            json={"schema_version": 1, "stories": stories},
        )

    def test_mounted_import_is_pending_reusable_only_after_review(self):
        response = self._post([story()])
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        self.assertEqual(receipt["created_count"], 1)
        [imported] = receipt["stories"]
        self.assertEqual(imported["review_status"], "PENDING")
        self.assertFalse(imported["reusable"])
        self.assertEqual(imported["import_origin"]["canonical_sha256"],
                         receipt["canonical_sha256"])
        self.assertEqual(
            select_stories_for_job(self.store, "fixture", "fixture-job", "RESUME")["stories"],
            [],
        )
        with self.assertRaises(StorySelectionError) as blocked:
            select_stories_for_job(
                self.store, "fixture", "fixture-job", "RESUME", [imported["story_id"]]
            )
        self.assertEqual(blocked.exception.code, "STORY_NOT_SELECTABLE")
        listed = self.client.get("/profiles/fixture/career-stories")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["stories"][0]["story_id"], imported["story_id"])
        approved = self.client.post(
            f"/profiles/fixture/career-stories/{imported['story_id']}/review",
            json={"approved": True, "expected_revision": 1, "reviewer_note": "Verified"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertTrue(approved.json()["reusable"])
        replay = self._post([story()])
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["created_count"], 0)
        self.assertEqual(replay.json()["stories"][0]["review_status"], "APPROVED")

    def test_invalid_or_oversized_requests_leave_no_rows(self):
        path = "/profiles/fixture/career-stories/import"
        cases = [
            self.client.post(path, content=b"not json",
                             headers={"content-type": "application/json"}),
            self.client.post(path, content=b'{"schema_version":1,"schema_version":1,"stories":[]}',
                             headers={"content-type": "application/json"}),
            self.client.post(path, content=b'{"schema_version":1,"stories":{},"approved":true}',
                             headers={"content-type": "application/json"}),
            self.client.post(
                path,
                content=b'{"schema_version":1,"stories":[' + b"9" * 5000 + b"]}",
                headers={"content-type": "application/json"},
            ),
            self._post([story(), story(fact_ids=["missing"])]),
            self._post([story(approved=True)]),
        ]
        for response in cases:
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.client.get("/profiles/fixture/career-stories").json()["stories"], [])
        self.assertEqual(cases[3].json()["detail"]["code"], "INVALID_IMPORT")
        too_large = self.client.post(path, content=b"x" * (256 * 1024 + 1),
                                     headers={"content-type": "application/json"})
        self.assertEqual(too_large.status_code, 413, too_large.text)
        self.assertEqual(too_large.json()["detail"]["code"], "IMPORT_TOO_LARGE")
        wrong_media = self.client.post(path, content=b"{}", headers={"content-type": "text/plain"})
        self.assertEqual(wrong_media.status_code, 415, wrong_media.text)

    def test_reopened_store_preserves_origin_and_replay(self):
        first = self._post([story()]).json()["stories"][0]
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.store.migrate()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root, operation_mode="PERSONAL"))
        [persisted] = self.client.get("/profiles/fixture/career-stories").json()["stories"]
        self.assertEqual(persisted["import_origin"], first["import_origin"])
        self.assertEqual(persisted["text"], first["text"])
        self.assertEqual(self._post([story()]).json()["created_count"], 0)
