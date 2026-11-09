"""Atomic career-story import and durable replay semantics."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from packages.persistence.store import Store
from services.career_profile.story_bank import StoryBankError, list_stories, review_story, revise_story
from services.career_profile.story_import import import_stories, parse_story_import
from tests.unit.test_career_story_bank import PROFILE, payload


def document(*stories):
    return json.dumps({"schema_version": 1, "stories": list(stories)}).encode("utf-8")


class CareerStoryImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "stories.sqlite"
        self.url = f"sqlite:///{self.database.as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_three_kinds_are_pending_with_durable_origin_and_canonical_hash(self):
        data = document(
            payload(kind="STAR_STORY"),
            payload(kind="ACCOMPLISHMENT", title="An accomplishment"),
            payload(kind="RESUME_BULLET", title="A resume bullet"),
        )
        imported = import_stories(self.store, "story-owner", data)
        self.assertEqual(imported["created_count"], 3)
        self.assertEqual([s["kind"] for s in imported["stories"]],
                         ["STAR_STORY", "ACCOMPLISHMENT", "RESUME_BULLET"])
        self.assertTrue(all(s["review_status"] == "PENDING" and not s["reusable"]
                            for s in imported["stories"]))
        for index, story in enumerate(imported["stories"]):
            self.assertEqual(story["import_origin"], {
                "schema_version": 1,
                "canonical_sha256": imported["canonical_sha256"],
                "record_index": index,
            })
        # JSON layout and key ordering do not change normalized import identity.
        reordered = json.dumps(json.loads(data), indent=2).encode("utf-8")
        again = import_stories(self.store, "story-owner", reordered)
        self.assertEqual(again["canonical_sha256"], imported["canonical_sha256"])
        self.assertEqual(again["created_count"], 0)
        self.assertEqual([s["story_id"] for s in again["stories"]],
                         [s["story_id"] for s in imported["stories"]])
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.store.migrate()
        self.assertEqual(
            {s["story_id"]: s["import_origin"] for s in list_stories(self.store, "story-owner")},
            {s["story_id"]: s["import_origin"] for s in imported["stories"]},
        )

    def test_invalid_second_story_rolls_back_first(self):
        data = document(payload(), payload(fact_ids=["missing"]))
        with self.assertRaises(StoryBankError) as raised:
            import_stories(self.store, "story-owner", data)
        self.assertEqual(raised.exception.code, "FACTS_UNAVAILABLE")
        self.assertEqual(list_stories(self.store, "story-owner"), [])

    def test_replay_preserves_review_and_pending_edit(self):
        data = document(payload())
        [first] = import_stories(self.store, "story-owner", data)["stories"]
        approved = review_story(self.store, "story-owner", first["story_id"],
                                True, "Checked", 1)
        self.assertTrue(approved["reusable"])
        [replay] = import_stories(self.store, "story-owner", data)["stories"]
        self.assertEqual(replay["story_id"], first["story_id"])
        self.assertEqual(replay["review_status"], "APPROVED")
        edited = revise_story(self.store, "story-owner", first["story_id"],
                              payload(text="Edited account of the same accomplishment."), 1)
        self.assertEqual(edited["review_status"], "PENDING")
        [replay] = import_stories(self.store, "story-owner", data)["stories"]
        self.assertEqual(replay["story_id"], first["story_id"])
        self.assertFalse(replay["is_latest"])
        self.assertFalse(replay["reusable"])
        self.assertEqual(replay["text"], first["text"])
        self.assertEqual(len(list_stories(self.store, "story-owner")), 2)

    def test_foreign_or_stale_facts_reject_batch(self):
        self.store.save_profile(dict(PROFILE, id="other-owner"))
        foreign = dict(PROFILE, id="other-owner", facts=[])
        self.store.save_profile(foreign)
        with self.assertRaises(StoryBankError) as raised:
            import_stories(self.store, "other-owner", document(payload()))
        self.assertEqual(raised.exception.code, "FACTS_UNAVAILABLE")
        first = import_stories(self.store, "story-owner", document(payload()))
        changed = dict(PROFILE, facts=[
            dict(PROFILE["facts"][0], text="Changed fact"),
            PROFILE["facts"][1],
        ])
        self.store.save_profile(changed)
        with self.assertRaises(StoryBankError) as raised:
            import_stories(self.store, "story-owner", document(payload()))
        self.assertEqual(raised.exception.code, "FACT_FINGERPRINT_MISMATCH")
        self.assertEqual(len(list_stories(self.store, "story-owner")), 1)
        self.assertEqual(first["stories"][0]["review_status"], "PENDING")

    def test_unapproved_current_fact_blocks_import(self):
        changed = dict(PROFILE, facts=[
            dict(PROFILE["facts"][0], user_approved=False),
            PROFILE["facts"][1],
        ])
        self.store.save_profile(changed)
        with self.assertRaises(StoryBankError) as raised:
            import_stories(self.store, "story-owner", document(payload()))
        self.assertEqual(raised.exception.code, "FACT_NOT_APPROVED")
        self.assertEqual(list_stories(self.store, "story-owner"), [])

    def test_unsafe_text_has_stable_import_error(self):
        for text in ("Unsafe \x00 text", "Unsafe \ud800 text"):
            with self.subTest(text=repr(text)), self.assertRaises(StoryBankError) as raised:
                parse_story_import(document(payload(text=text)))
            self.assertEqual(raised.exception.code, "INVALID_IMPORT")

    def test_oversized_integer_inside_byte_cap_is_invalid_import(self):
        raw = b'{"schema_version":1,"stories":[' + b"9" * 5000 + b"]}"
        with self.assertRaises(StoryBankError) as raised:
            import_stories(self.store, "story-owner", raw)
        self.assertEqual(raised.exception.code, "INVALID_IMPORT")
        self.assertEqual(list_stories(self.store, "story-owner"), [])

    def test_strict_json_and_story_shape(self):
        invalid = [
            b'{"schema_version":1,"schema_version":1,"stories":[]}',
            b'{"schema_version":NaN,"stories":[]}',
            b'{"schema_version":true,"stories":[]}',
            b'{"schema_version":2,"stories":[]}',
            b'{"schema_version":1,"stories":[]}',
            b'{"schema_version":1,"stories":[{"kind":"STAR_STORY","kind":"STAR_STORY"}]}',
            document(payload(review_status="APPROVED")),
            document(payload(text="Unsafe \x00 text")),
            document(payload(text="Unsafe \ud800 text")),
            document(*[payload(title=str(i)) for i in range(21)]),
            b"\xff",
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:50]), self.assertRaises(StoryBankError):
                parse_story_import(raw)
