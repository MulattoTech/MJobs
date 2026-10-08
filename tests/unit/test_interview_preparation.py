"""Deterministic, persisted interview preparation uses only current story revisions."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, JSON, String, Table, Text, UniqueConstraint

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.career_profile.interview_preparation import (
    InterviewPreparationError,
    create_interview_prep_pack,
    get_interview_prep_pack,
    list_interview_prep_packs,
)
from services.career_profile.story_bank import create_story, revise_story, review_story


# Keep this service slice testable on its assigned base while the separately
# owned migration is being assembled. The integrated model is used when present.
if not hasattr(m, "interview_prep_packs"):
    m.interview_prep_packs = Table(
        "interview_prep_packs", m.metadata,
        Column("id", String(128), primary_key=True),
        Column("profile_id", ForeignKey("career_profiles.id"), nullable=False),
        Column("job_id", ForeignKey("canonical_jobs.id"), nullable=False),
        Column("pack_version", Integer, nullable=False),
        Column("profile_version", String(64), nullable=False),
        Column("job_fingerprint", String(64), nullable=False),
        Column("selected_story_revision_ids", JSON, nullable=False),
        Column("questions", JSON, nullable=False),
        Column("talking_points", JSON, nullable=False),
        Column("star_prompts", JSON, nullable=False),
        Column("missing_evidence", JSON, nullable=False),
        Column("export_text", Text, nullable=False),
        Column("created_at", String(64), nullable=False),
        UniqueConstraint("profile_id", "job_id", "pack_version",
                         name="uq_interview_prep_profile_job_version"),
        CheckConstraint("pack_version >= 1", name="ck_interview_prep_pack_version_positive"),
    )


PROFILE = {
    "id": "prep-owner", "name": "Alex Example", "email": "alex@example.test",
    "facts": [{
        "id": "fact-automation", "kind": "accomplishment",
        "text": "Automated Windows Server deployments with PowerShell.",
        "source": "reviewed resume revision 3", "verified": True,
        "user_approved": True, "confidence": 96,
    }],
    "answers": [],
}


class InterviewPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'prep.sqlite').as_posix()}")
        self.store.migrate()
        self.store.save_profile(PROFILE)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies,
                   {"id": "prep-company", "name": "Example Systems", "domain": "example.test"})
            upsert(connection, m.jobs, {
                "id": "prep-job", "company_id": "prep-company", "requisition": "PREP-1",
                "title": "Systems Administrator",
                "description": "Responsibilities: Maintain Windows Server and automate deployments.",
                "skills": ["Windows Server", "PowerShell"],
                "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
        story = create_story(self.store, "prep-owner", {
            "kind": "STAR_STORY", "title": "Automated Windows Server deployments",
            "text": "Situation: deployments were manual. Task: improve consistency. "
                    "Action: automated Windows Server deployment with PowerShell. "
                    "Result: deployments became repeatable.",
            "fact_ids": ["fact-automation"], "tags": ["automation"],
            "allowed_uses": ["INTERVIEW_PREPARATION"],
        })
        self.story = review_story(
            self.store, "prep-owner", story["story_id"], True, "Verified by user", 1
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_pack_uses_only_selected_current_story_and_exports_deterministically(self):
        pack = create_interview_prep_pack(
            self.store, "prep-owner", "prep-job", [self.story["story_id"]]
        )

        self.assertEqual(pack["schema_version"], 1)
        self.assertEqual(pack["profile_version"], self.store.profile("prep-owner")["version"])
        self.assertEqual(pack["selected_story_revision_ids"], [self.story["story_id"]])
        self.assertTrue(pack["job_fingerprint"])
        self.assertTrue(pack["questions"])
        self.assertTrue(pack["talking_points"])
        self.assertTrue(pack["star_prompts"])
        self.assertTrue(all(item["source_refs"] for item in pack["questions"]))
        self.assertTrue(all(item["source_refs"] for item in pack["talking_points"]))
        self.assertTrue(all(item["source_refs"] for item in pack["star_prompts"]))
        self.assertIn("Situation:", pack["talking_points"][0]["text"])
        self.assertNotIn("answer", {key.casefold() for key in pack["star_prompts"][0]})
        self.assertEqual(
            pack["export_text"],
            create_interview_prep_pack(
                self.store, "prep-owner", "prep-job", [self.story["story_id"]]
            )["export_text"],
        )

    def test_pack_is_idempotent_for_same_inputs_and_versions_changed_inputs(self):
        first = create_interview_prep_pack(
            self.store, "prep-owner", "prep-job", [self.story["story_id"]]
        )
        repeated = create_interview_prep_pack(
            self.store, "prep-owner", "prep-job", [self.story["story_id"]]
        )
        self.assertEqual(first["pack_id"], repeated["pack_id"])
        self.assertEqual(first["pack_version"], 1)

        changed = create_interview_prep_pack(self.store, "prep-owner", "prep-job", [])
        self.assertEqual(changed["pack_version"], 2)
        self.assertNotEqual(changed["pack_id"], first["pack_id"])
        self.assertEqual(len(list_interview_prep_packs(self.store, "prep-owner", "prep-job")), 2)

    def test_returning_to_older_selection_appends_a_new_version(self):
        first = create_interview_prep_pack(
            self.store, "prep-owner", "prep-job", [self.story["story_id"]]
        )
        create_interview_prep_pack(self.store, "prep-owner", "prep-job", [])

        returned = create_interview_prep_pack(
            self.store, "prep-owner", "prep-job", [self.story["story_id"]]
        )

        self.assertEqual(returned["pack_version"], 3)
        self.assertNotEqual(returned["pack_id"], first["pack_id"])
        self.assertEqual(len(list_interview_prep_packs(self.store, "prep-owner", "prep-job")), 3)

    def test_missing_evidence_is_explicit_and_packs_read_back_by_profile_and_job(self):
        pack = create_interview_prep_pack(self.store, "prep-owner", "prep-job", [])

        self.assertTrue(pack["missing_evidence"])
        self.assertTrue(all(item.get("topic") and item.get("reason")
                            for item in pack["missing_evidence"]))
        self.assertEqual(
            get_interview_prep_pack(
                self.store, "prep-owner", "prep-job", pack["pack_id"]
            ),
            pack,
        )
        self.assertIsNone(get_interview_prep_pack(
            self.store, "other-profile", "prep-job", pack["pack_id"]
        ))

    def test_collection_read_rejects_missing_job(self):
        with self.assertRaises(InterviewPreparationError) as raised:
            list_interview_prep_packs(self.store, "prep-owner", "missing-job")

        self.assertEqual(raised.exception.code, "JOB_NOT_FOUND")
        self.assertEqual(raised.exception.status_code, 404)

    def test_pack_survives_store_reopen(self):
        pack = create_interview_prep_pack(
            self.store, "prep-owner", "prep-job", [self.story["story_id"]]
        )
        database_url = self.store.url
        self.store.engine.dispose()
        reopened = Store(database_url)
        reopened.migrate()
        self.store = reopened

        readback = get_interview_prep_pack(
            reopened, "prep-owner", "prep-job", pack["pack_id"]
        )

        self.assertEqual(readback, pack)
        self.assertEqual(
            [item["pack_id"] for item in list_interview_prep_packs(
                reopened, "prep-owner", "prep-job"
            )],
            [pack["pack_id"]],
        )

    def test_pending_or_nonlatest_story_revision_cannot_be_selected(self):
        pending = create_story(self.store, "prep-owner", {
            "kind": "STAR_STORY", "title": "Pending story", "text": "Pending user authored text.",
            "fact_ids": ["fact-automation"], "tags": [],
            "allowed_uses": ["INTERVIEW_PREPARATION"],
        })

        with self.assertRaises(InterviewPreparationError) as raised:
            create_interview_prep_pack(
                self.store, "prep-owner", "prep-job", [pending["story_id"]]
            )

        self.assertEqual(raised.exception.code, "STORY_NOT_SELECTABLE")

    def test_approved_superseded_story_revision_is_rejected(self):
        old_revision = self.story["story_id"]
        revise_story(self.store, "prep-owner", old_revision, {
            "kind": "STAR_STORY", "title": "Updated deployment story",
            "text": "Situation: deployments were manual. Task: improve consistency. "
                    "Action: documented the deployment. Result: the process was clearer.",
            "fact_ids": ["fact-automation"], "tags": ["automation"],
            "allowed_uses": ["INTERVIEW_PREPARATION"],
        }, expected_revision=1)

        with self.assertRaises(InterviewPreparationError) as raised:
            create_interview_prep_pack(
                self.store, "prep-owner", "prep-job", [old_revision]
            )

        self.assertEqual(raised.exception.code, "STORY_NOT_SELECTABLE")

    def test_profile_or_job_context_change_appends_a_new_pack_version(self):
        first = create_interview_prep_pack(self.store, "prep-owner", "prep-job", [])
        with self.store.engine.begin() as connection:
            connection.execute(
                m.jobs.update().where(m.jobs.c.id == "prep-job")
                .values(title="Senior Systems Administrator")
            )
        changed_job = create_interview_prep_pack(self.store, "prep-owner", "prep-job", [])
        self.assertEqual(changed_job["pack_version"], 2)
        self.assertNotEqual(changed_job["job_fingerprint"], first["job_fingerprint"])

        self.store.save_profile({**PROFILE, "name": "Alex Updated"})
        changed_profile = create_interview_prep_pack(
            self.store, "prep-owner", "prep-job", []
        )
        self.assertEqual(changed_profile["pack_version"], 3)
        self.assertNotEqual(changed_profile["profile_version"], changed_job["profile_version"])


if __name__ == "__main__":
    unittest.main()
