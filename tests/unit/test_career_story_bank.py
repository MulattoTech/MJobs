"""Immutable career-story revisions linked to approved career facts."""
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
    revise_story,
)


PROFILE = {
    "id": "story-owner",
    "name": "Alex Fixture",
    "email": "alex@example.test",
    "facts": [
        {"id": "project-x", "text": "Delivered Project X", "kind": "accomplishment",
         "source": "fictional-test-profile", "verified": True, "user_approved": True,
         "confidence": 95},
        {"id": "python", "text": "Python", "kind": "skill",
         "source": "fictional-test-profile", "verified": True, "user_approved": True,
         "confidence": 100},
    ],
    "answers": [],
}


def payload(**overrides):
    value = {
        "kind": "STAR_STORY",
        "title": "Improved a test deployment",
        "text": "Situation: releases were slow. Task: reduce deployment time. Action: automated checks. Result: reduced release time.",
        "fact_ids": ["project-x", "python"],
        "tags": ["automation", "delivery"],
        "allowed_uses": ["RESUME", "INTERVIEW_PREPARATION"],
    }
    value.update(overrides)
    return value


class CareerStoryBankTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "stories.sqlite"
        self.store = Store(f"sqlite:///{path.as_posix()}")
        self.store.migrate()
        self.profile_version = self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_new_story_is_profile_scoped_pending_and_not_reusable(self):
        story = create_story(self.store, "story-owner", payload())

        self.assertEqual(story["profile_id"], "story-owner")
        self.assertEqual(story["profile_version"], self.profile_version)
        self.assertEqual(story["fact_ids"], ["project-x", "python"])
        self.assertEqual(story["revision"], 1)
        self.assertEqual(story["review_status"], "PENDING")
        self.assertTrue(story["is_latest"])
        self.assertFalse(story["reusable"])
        self.assertEqual(story["stale_fact_ids"], [])
        self.assertEqual(len(story["fact_fingerprints"]), 2)

    def test_story_requires_unique_current_verified_approved_facts(self):
        rejected = [
            ("missing", ["not-current"], "FACTS_UNAVAILABLE"),
            ("duplicate", ["python", "python"], "INVALID_STORY"),
        ]
        for name, fact_ids, code in rejected:
            with self.subTest(name=name), self.assertRaises(StoryBankError) as raised:
                create_story(self.store, "story-owner", payload(fact_ids=fact_ids))
            self.assertEqual(raised.exception.code, code)

        for name, changes in (
            ("unverified", {"verified": False}),
            ("unapproved", {"user_approved": False}),
            ("low confidence", {"confidence": 79}),
            ("empty source", {"source": "  "}),
            ("empty text", {"text": "  "}),
        ):
            with self.subTest(name=name):
                changed_profile = dict(PROFILE, facts=[
                    dict(PROFILE["facts"][0], **changes), PROFILE["facts"][1]
                ])
                self.store.save_profile(changed_profile)
                with self.assertRaises(StoryBankError) as raised:
                    create_story(self.store, "story-owner", payload(fact_ids=["project-x"]))
                self.assertEqual(raised.exception.code, "FACT_NOT_APPROVED")
                self.store.save_profile(PROFILE)

    def test_create_with_unknown_profile_fails_without_cross_profile_fact_access(self):
        with self.assertRaises(StoryBankError) as raised:
            create_story(self.store, "other-profile", payload(fact_ids=["python"]))
        self.assertEqual(raised.exception.code, "PROFILE_NOT_FOUND")

    def test_review_approves_once_and_only_latest_current_revision_is_reusable(self):
        first = create_story(self.store, "story-owner", payload())
        approved = review_story(
            self.store, "story-owner", first["story_id"], True, "Reviewed against approved facts", 1
        )
        self.assertEqual(approved["review_status"], "APPROVED")
        self.assertTrue(approved["reusable"])

        with self.assertRaises(StoryBankError) as raised:
            review_story(self.store, "story-owner", first["story_id"], False, "second decision", 1)
        self.assertEqual(raised.exception.code, "REVIEW_ALREADY_FINAL")

        revised = revise_story(
            self.store, "story-owner", first["story_id"],
            payload(text="Updated user-authored accomplishment wording."), 1,
        )
        self.assertEqual(revised["story_key"], first["story_key"])
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["review_status"], "PENDING")
        self.assertFalse(revised["reusable"])
        rows = list_stories(self.store, "story-owner")
        self.assertEqual([row["revision"] for row in rows], [2, 1])
        self.assertEqual([row["is_latest"] for row in rows], [True, False])
        self.assertFalse(any(row["reusable"] for row in rows))

    def test_profile_change_makes_approved_story_stale_and_nonreusable(self):
        first = create_story(self.store, "story-owner", payload(fact_ids=["project-x"]))
        review_story(self.store, "story-owner", first["story_id"], True, "Reviewed", 1)
        changed = dict(PROFILE, facts=[
            dict(PROFILE["facts"][0], text="Project X was delivered with a revised scope."),
            PROFILE["facts"][1],
        ])
        self.store.save_profile(changed)

        [current] = list_stories(self.store, "story-owner")
        self.assertFalse(current["reusable"])
        self.assertEqual(current["stale_fact_ids"], ["project-x"])

    def test_story_operations_are_scoped_to_the_owning_profile(self):
        story = create_story(self.store, "story-owner", payload())
        self.store.save_profile(dict(PROFILE, id="someone-else"))
        with self.assertRaises(StoryBankError) as raised:
            revise_story(self.store, "someone-else", story["story_id"], payload(), 1)
        self.assertEqual(raised.exception.code, "STORY_NOT_FOUND")
        with self.assertRaises(StoryBankError) as raised:
            review_story(self.store, "someone-else", story["story_id"], True, "Reviewed", 1)
        self.assertEqual(raised.exception.code, "STORY_NOT_FOUND")
        self.assertEqual(list_stories(self.store, "someone-else"), [])

    def test_story_input_bounds_and_allowed_use_values_are_enforced(self):
        cases = [
            ("unsupported kind", {"kind": "GENERATED"}),
            ("unsupported use", {"allowed_uses": ["AUTO_SUBMIT"]}),
            ("too many uses", {"allowed_uses": ["RESUME", "COVER_LETTER", "APPLICATION_ANSWER", "INTERVIEW_PREPARATION", "RESUME"]}),
            ("duplicate tags", {"tags": ["same", "same"]}),
            ("empty title", {"title": "  "}),
            ("empty text", {"text": "  "}),
        ]
        for name, changes in cases:
            with self.subTest(name=name), self.assertRaises(StoryBankError):
                create_story(self.store, "story-owner", payload(**changes))

    def test_stale_expected_revision_cannot_overwrite_or_review_content(self):
        story = create_story(self.store, "story-owner", payload())
        with self.assertRaises(StoryBankError) as raised:
            revise_story(self.store, "story-owner", story["story_id"], payload(title="Changed"), 7)
        self.assertEqual(raised.exception.code, "REVISION_CONFLICT")
        with self.assertRaises(StoryBankError) as raised:
            review_story(self.store, "story-owner", story["story_id"], True, "Reviewed", 7)
        self.assertEqual(raised.exception.code, "REVISION_CONFLICT")
        [unchanged] = list_stories(self.store, "story-owner")
        self.assertEqual(unchanged["title"], story["title"])
        self.assertEqual(unchanged["revision"], 1)


if __name__ == "__main__":
    unittest.main()
