"""Job-specific selection of reviewed, current career-story revisions."""
from pathlib import Path
import tempfile
import unittest

from packages.persistence.store import Store
from services.career_profile.story_bank import create_story, review_story
from services.career_profile.story_selection import (
    StorySelectionError,
    select_stories_for_job,
)


PROFILE = {
    "id": "story-owner",
    "name": "Alex Fixture",
    "email": "alex@example.test",
    "facts": [{
        "id": "project-x", "text": "Delivered Project X", "kind": "accomplishment",
        "source": "fictional-test-profile", "verified": True, "user_approved": True,
        "confidence": 95,
    }],
    "answers": [],
}


def payload(**overrides):
    value = {
        "kind": "STAR_STORY",
        "title": "Improved a test deployment",
        "text": "Situation: releases were slow. Task: reduce deployment time. "
                "Action: automated checks. Result: reduced release time.",
        "fact_ids": ["project-x"],
        "tags": ["automation"],
        "allowed_uses": ["RESUME", "COVER_LETTER"],
    }
    return {**value, **overrides}


class CareerStorySelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "stories.sqlite"
        self.store = Store(f"sqlite:///{path.as_posix()}")
        self.store.migrate()
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _approved(self, **overrides):
        story = create_story(self.store, "story-owner", payload(**overrides))
        return review_story(
            self.store, "story-owner", story["story_id"], True, "Reviewed", 1
        )

    def test_job_picker_excludes_pending_stale_and_wrong_use_stories(self):
        pending = create_story(self.store, "story-owner", payload(title="Pending"))
        wrong_use = self._approved(
            title="Wrong use", allowed_uses=["INTERVIEW_PREPARATION"]
        )
        stale = self._approved(title="Stale")
        changed_profile = dict(PROFILE, facts=[
            dict(PROFILE["facts"][0], text="Project X delivery was revised.")
        ])
        self.store.save_profile(changed_profile)
        eligible = self._approved(title="Current")

        result = select_stories_for_job(
            self.store, "story-owner", "job-42", "RESUME"
        )

        self.assertEqual(result["job_id"], "job-42")
        self.assertEqual(result["allowed_use"], "RESUME")
        self.assertEqual([row["story_id"] for row in result["stories"]], [eligible["story_id"]])
        self.assertNotIn(pending["story_id"], result["story_ids"])
        self.assertNotIn(wrong_use["story_id"], result["story_ids"])
        self.assertNotIn(stale["story_id"], result["story_ids"])
        self.assertTrue(result["stories"][0]["reusable"])

    def test_candidate_order_is_stable_and_explicit_selection_keeps_user_order(self):
        stories = [self._approved(title=f"Story {index}") for index in range(3)]
        options = select_stories_for_job(
            self.store, "story-owner", "job-42", "COVER_LETTER"
        )
        repeated = select_stories_for_job(
            self.store, "story-owner", "job-42", "COVER_LETTER"
        )
        expected_order = sorted(row["story_id"] for row in stories)
        self.assertEqual(options["story_ids"], expected_order)
        self.assertEqual(repeated["story_ids"], expected_order)

        chosen_ids = [stories[2]["story_id"], stories[0]["story_id"]]
        chosen = select_stories_for_job(
            self.store, "story-owner", "job-42", "COVER_LETTER", chosen_ids
        )
        self.assertEqual(chosen["story_ids"], chosen_ids)
        self.assertEqual([row["story_id"] for row in chosen["stories"]], chosen_ids)
        self.assertEqual(chosen["stories"][0]["fact_ids"], ["project-x"])
        self.assertEqual(chosen["stories"][0]["fact_fingerprints"], stories[2]["fact_fingerprints"])
        self.assertEqual(chosen["stories"][0]["provenance"][0]["fact_id"], "project-x")

    def test_explicit_selection_rejects_an_ineligible_story_instead_of_silently_dropping_it(self):
        pending = create_story(self.store, "story-owner", payload())

        with self.assertRaises(StorySelectionError) as raised:
            select_stories_for_job(
                self.store, "story-owner", "job-42", "RESUME", [pending["story_id"]]
            )

        self.assertEqual(raised.exception.code, "STORY_NOT_SELECTABLE")

    def test_picker_rejects_invalid_use_and_cross_profile_selection(self):
        story = self._approved()

        with self.assertRaises(StorySelectionError) as raised:
            select_stories_for_job(self.store, "story-owner", "job-42", "APPLICATION_ANSWER")
        self.assertEqual(raised.exception.code, "INVALID_ALLOWED_USE")

        self.store.save_profile({**PROFILE, "id": "another-profile"})
        with self.assertRaises(StorySelectionError) as raised:
            select_stories_for_job(
                self.store, "another-profile", "job-42", "RESUME", [story["story_id"]]
            )
        self.assertEqual(raised.exception.code, "STORY_NOT_SELECTABLE")

    def test_fact_that_loses_user_approval_is_excluded_without_fact_mutation(self):
        approved = self._approved()
        before = self.store.profile("story-owner")
        self.store.save_profile(dict(PROFILE, facts=[
            dict(PROFILE["facts"][0], user_approved=False)
        ]))
        after_revocation = self.store.profile("story-owner")

        result = select_stories_for_job(
            self.store, "story-owner", "job-42", "RESUME"
        )

        self.assertEqual(result["stories"], [])
        self.assertEqual(result["story_ids"], [])
        current = self.store.profile("story-owner")
        self.assertEqual(current["facts"], after_revocation["facts"])
        self.assertEqual(current["version"], after_revocation["version"])
        self.assertTrue(before["facts"][0]["user_approved"])
        self.assertEqual(approved["fact_ids"], ["project-x"])


if __name__ == "__main__":
    unittest.main()
