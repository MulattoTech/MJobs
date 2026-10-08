"""Deterministic role lenses reorder only current, approved profile evidence."""
from pathlib import Path
import copy
import tempfile
import unittest

from packages.persistence.store import Store
from services.career_profile.role_lenses import (
    RoleLensError,
    list_role_lenses,
    select_role_lens,
)
from services.career_profile.story_bank import create_story, review_story, revise_story


PROFILE = {
    "id": "lens-owner",
    "name": "Alex Fixture",
    "email": "alex@example.test",
    "facts": [
        {
            "id": "support-fact",
            "text": "Resolved help desk incidents and improved user support workflows.",
            "kind": "responsibility",
            "source": "fictional-test-profile",
            "verified": True,
            "user_approved": True,
            "confidence": 95,
        },
        {
            "id": "systems-fact",
            "text": "Administered Linux servers and maintained network infrastructure.",
            "kind": "technology_experience",
            "source": "fictional-test-profile",
            "verified": True,
            "user_approved": True,
            "confidence": 95,
        },
        {
            "id": "cloud-fact",
            "text": "Deployed workloads to AWS and automated Kubernetes operations.",
            "kind": "project",
            "source": "fictional-test-profile",
            "verified": True,
            "user_approved": True,
            "confidence": 95,
        },
        {
            "id": "security-fact",
            "text": "Triaged security vulnerabilities and improved identity access controls.",
            "kind": "accomplishment",
            "source": "fictional-test-profile",
            "verified": True,
            "user_approved": True,
            "confidence": 95,
        },
        {
            "id": "unapproved-fact",
            "text": "Unreviewed claim about cloud security.",
            "kind": "accomplishment",
            "source": "fictional-test-profile",
            "verified": True,
            "user_approved": False,
            "confidence": 99,
        },
        {
            "id": "unverified-fact",
            "text": "Unverified systems administration claim.",
            "kind": "experience",
            "source": "fictional-test-profile",
            "verified": False,
            "user_approved": True,
            "confidence": 99,
        },
    ],
    "answers": [],
}


def story_payload(title, text, fact_id, allowed_uses=None):
    return {
        "kind": "STAR_STORY",
        "title": title,
        "text": text,
        "fact_ids": [fact_id],
        "tags": ["lens-test"],
        "allowed_uses": allowed_uses or ["RESUME", "COVER_LETTER"],
    }


class RoleLensTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "role-lenses.sqlite"
        self.store = Store(f"sqlite:///{path.as_posix()}")
        self.store.migrate()
        self.store.save_profile(copy.deepcopy(PROFILE))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _approved_story(self, title, text, fact_id, allowed_uses=None):
        pending = create_story(
            self.store, "lens-owner",
            story_payload(title, text, fact_id, allowed_uses),
        )
        return review_story(
            self.store, "lens-owner", pending["story_id"], True, "Reviewed", 1
        )

    def test_versioned_registry_exposes_five_occupation_agnostic_lenses(self):
        rows = list_role_lenses()

        self.assertEqual(
            [row["id"] for row in rows],
            ["support", "systems", "cloud", "security", "general"],
        )
        self.assertTrue(all(row["version"] == 1 for row in rows))
        self.assertTrue(all(row["display_name"] for row in rows))

    def test_role_text_selects_deterministic_support_systems_cloud_security_or_general_lens(self):
        cases = [
            ("Technical Support Specialist", "support"),
            ("Systems Administrator", "systems"),
            ("Cloud Platform Engineer", "cloud"),
            ("Cybersecurity Analyst", "security"),
            ("Unrelated Occupation", "general"),
        ]

        for role_text, expected_lens_id in cases:
            with self.subTest(role_text=role_text):
                first = select_role_lens(self.store, "lens-owner", role_text)
                second = select_role_lens(self.store, "lens-owner", role_text)
                self.assertEqual(first, second)
                self.assertEqual(first["lens"]["id"], expected_lens_id)
                self.assertEqual(first["lens"]["version"], 1)

    def test_two_lenses_reorder_and_emphasize_the_same_approved_facts_with_provenance(self):
        before = self.store.profile("lens-owner")

        support = select_role_lens(self.store, "lens-owner", "Help Desk Technician")
        cloud = select_role_lens(self.store, "lens-owner", "Cloud Engineer")

        expected_ids = {"support-fact", "systems-fact", "cloud-fact", "security-fact"}
        self.assertEqual(set(support["fact_ids"]), expected_ids)
        self.assertEqual(set(cloud["fact_ids"]), expected_ids)
        self.assertNotEqual(support["fact_ids"], cloud["fact_ids"])
        self.assertEqual(support["emphasized_fact_ids"][0], "support-fact")
        self.assertEqual(cloud["emphasized_fact_ids"][0], "cloud-fact")
        self.assertNotIn("unapproved-fact", support["fact_ids"])
        self.assertNotIn("unverified-fact", support["fact_ids"])
        self.assertEqual(
            {row["fact_id"] for row in support["source_refs"]["facts"]}, expected_ids
        )
        self.assertTrue(all(row["fingerprint"] for row in support["source_refs"]["facts"]))
        self.assertEqual(self.store.profile("lens-owner"), before)

    def test_story_refs_include_only_current_approved_fact_linked_revisions_and_exact_text(self):
        current = self._approved_story(
            "Cloud operations story",
            "Automated Kubernetes operations for reliable cloud deployments.",
            "cloud-fact",
        )
        interview_only = self._approved_story(
            "Interview story",
            "Explained an AWS design during a technical interview.",
            "cloud-fact",
            ["INTERVIEW_PREPARATION"],
        )
        pending = create_story(
            self.store, "lens-owner",
            story_payload("Pending support story", "Resolved a support incident.", "support-fact"),
        )
        stale = self._approved_story(
            "Superseded support story", "Resolved an older support incident.", "support-fact"
        )
        revised = revise_story(
            self.store, "lens-owner", stale["story_id"],
            story_payload("Current support story", "Resolved a current help desk incident.", "support-fact"),
            1,
        )

        lens = select_role_lens(self.store, "lens-owner", "Cloud Engineer")

        self.assertEqual([story["story_id"] for story in lens["stories"]], [current["story_id"]])
        self.assertEqual(lens["stories"][0]["text"], current["text"])
        self.assertEqual(lens["story_ids"], [current["story_id"]])
        self.assertEqual(lens["source_refs"]["stories"], [{
            "story_id": current["story_id"],
            "story_key": current["story_key"],
            "revision": current["revision"],
            "profile_version": current["profile_version"],
            "fact_ids": current["fact_ids"],
            "fact_fingerprints": current["fact_fingerprints"],
        }])
        self.assertNotIn(interview_only["story_id"], lens["story_ids"])
        self.assertNotIn(pending["story_id"], lens["story_ids"])
        self.assertNotIn(stale["story_id"], lens["story_ids"])
        self.assertEqual(revised["revision"], 2)

    def test_revoked_fact_removes_fact_and_story_refs(self):
        story = self._approved_story(
            "Support story", "Resolved a help desk support incident.", "support-fact"
        )
        profile = copy.deepcopy(PROFILE)
        next(row for row in profile["facts"] if row["id"] == "support-fact")["user_approved"] = False
        self.store.save_profile(profile)
        saved_facts = self.store.profile("lens-owner")["facts"]

        lens = select_role_lens(self.store, "lens-owner", "Help Desk Technician")

        self.assertNotIn("support-fact", lens["fact_ids"])
        self.assertNotIn(story["story_id"], lens["story_ids"])
        self.assertEqual(self.store.profile("lens-owner")["facts"], saved_facts)

    def test_explicit_lens_overrides_deterministic_match_and_validates_id_and_version(self):
        explicit = select_role_lens(
            self.store, "lens-owner", "Cloud Engineer", role_lens_id="systems",
            expected_role_lens_version=1,
        )
        self.assertEqual(explicit["lens"]["id"], "systems")
        self.assertEqual(explicit["selection_reason"], "EXPLICIT")

        for role_lens_id in ("", "Systems", "x" * 65, 42):
            with self.subTest(role_lens_id=role_lens_id):
                with self.assertRaises(RoleLensError) as raised:
                    select_role_lens(self.store, "lens-owner", "Cloud Engineer",
                                     role_lens_id=role_lens_id)
                self.assertEqual(raised.exception.code, "ROLE_LENS_ID_INVALID")

        with self.assertRaises(RoleLensError) as unknown:
            select_role_lens(self.store, "lens-owner", "Cloud Engineer", role_lens_id="other")
        self.assertEqual(unknown.exception.code, "ROLE_LENS_NOT_FOUND")

        with self.assertRaises(RoleLensError) as malformed_version:
            select_role_lens(self.store, "lens-owner", "Cloud Engineer",
                             role_lens_id="cloud", expected_role_lens_version=True)
        self.assertEqual(malformed_version.exception.code, "ROLE_LENS_VERSION_INVALID")

        with self.assertRaises(RoleLensError) as stale_version:
            select_role_lens(self.store, "lens-owner", "Cloud Engineer",
                             role_lens_id="cloud", expected_role_lens_version=2)
        self.assertEqual(stale_version.exception.code, "ROLE_LENS_VERSION_CONFLICT")


if __name__ == "__main__":
    unittest.main()
