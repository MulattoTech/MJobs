"""Career stories must stay tied to the exact approved-fact snapshots reviewed."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from packages.persistence.store import Store
from services.career_profile.story_bank import (
    StoryBankError,
    create_story,
    list_stories,
    review_story,
)


PROFILE = {
    "id": "story-staleness-owner",
    "name": "Synthetic Story Owner",
    "email": "story-owner@example.test",
    "facts": [
        {
            "id": "fact-one",
            "text": "Reduced a synthetic deployment's recovery time.",
            "kind": "accomplishment",
            "source": "Synthetic fixture history",
            "verified": True,
            "user_approved": True,
            "confidence": 96,
        }
    ],
    "answers": [],
}


def story_payload():
    return {
        "kind": "STAR_STORY",
        "title": "Synthetic deployment recovery",
        "text": "I documented a recovery procedure and reduced repeat recovery time.",
        "fact_ids": ["fact-one"],
        "tags": ["recovery"],
        "allowed_uses": ["INTERVIEW_PREPARATION", "RESUME"],
    }


class CareerStoryBankStalenessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "stories.sqlite"
        self.store = Store(f"sqlite:///{path.as_posix()}")
        self.store.migrate()
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_pending_review_rejects_a_fact_revoked_after_story_creation(self):
        story = create_story(self.store, PROFILE["id"], story_payload())
        changed = dict(PROFILE, facts=[
            dict(PROFILE["facts"][0], verified=False, user_approved=False)
        ])
        self.store.save_profile(changed)

        with self.assertRaises(StoryBankError) as raised:
            review_story(
                self.store,
                PROFILE["id"],
                story["story_id"],
                True,
                "Reviewed after the linked fact changed",
                1,
            )

        self.assertEqual(raised.exception.code, "FACT_NOT_APPROVED")
        [still_pending] = list_stories(self.store, PROFILE["id"])
        self.assertEqual(still_pending["review_status"], "PENDING")
        self.assertFalse(still_pending["reusable"])
        self.assertEqual(still_pending["stale_fact_ids"], ["fact-one"])

    def test_pending_review_rejects_a_still_approved_fact_with_a_changed_fingerprint(self):
        story = create_story(self.store, PROFILE["id"], story_payload())
        changed = dict(PROFILE, facts=[
            dict(PROFILE["facts"][0], text="Updated synthetic recovery accomplishment.")
        ])
        self.store.save_profile(changed)

        with self.assertRaises(StoryBankError) as raised:
            review_story(
                self.store,
                PROFILE["id"],
                story["story_id"],
                True,
                "Reviewed against the prior fact wording",
                1,
            )

        self.assertEqual(raised.exception.code, "FACT_FINGERPRINT_MISMATCH")
        [still_pending] = list_stories(self.store, PROFILE["id"])
        self.assertEqual(still_pending["review_status"], "PENDING")
        self.assertFalse(still_pending["reusable"])
        self.assertEqual(still_pending["stale_fact_ids"], ["fact-one"])

    def test_approved_story_is_not_reusable_after_fact_provenance_fingerprint_changes(self):
        story = create_story(self.store, PROFILE["id"], story_payload())
        approved = review_story(
            self.store, PROFILE["id"], story["story_id"], True, "Reviewed", 1
        )
        self.assertTrue(approved["reusable"])

        changed = dict(PROFILE, facts=[
            dict(PROFILE["facts"][0], source="Updated synthetic fixture provenance")
        ])
        self.store.save_profile(changed)

        [stale] = list_stories(self.store, PROFILE["id"])
        self.assertEqual(stale["review_status"], "APPROVED")
        self.assertFalse(stale["reusable"])
        self.assertEqual(stale["stale_fact_ids"], ["fact-one"])


if __name__ == "__main__":
    unittest.main()
