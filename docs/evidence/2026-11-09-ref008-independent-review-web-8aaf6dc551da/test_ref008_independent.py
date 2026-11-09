"""Independent synthetic review probes for PR991 exact ca6a66fa."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.interview_preparation import create_interview_preparation_router
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.career_profile.interview_preparation import (
    InterviewPreparationError,
    _has_topic_evidence,
    create_interview_prep_pack,
    get_interview_prep_pack,
    list_interview_prep_packs,
)
from services.career_profile.story_bank import create_story, review_story


class IndependentIdentifierTests(unittest.TestCase):
    def test_new_literal_boundaries_do_not_cross_source_identity(self):
        cases = [
            ("Java", "JavaScript first, then Java.", True),
            ("C++", "C+++ is a different literal identifier.", False),
            ("C#", "AC# and C#X are unrelated identifiers.", False),
            ("Go", "Go2 and myGoModule were used.", False),
            ("SQL", "SQL_warehouse was the stored identifier.", False),
            ("C", "Objective\u2014C tools were used.", False),
            ("C", "Objective\u2013C tools were used.", False),
            ("Java", "prefix.Java was the stored identifier.", False),
            ("Java", "Java\u0308 was the exact name.", False),
            ("Rust", "Rust\u0327 was the exact name.", False),
            ("React Native", "React\u00a0\tNative was used.", True),
            ("Straße", "STRASSE was the exact reviewed word.", True),
            ("Café", "CAFÉ and other systems were maintained.", True),
            ("F#", "(F#), Rust and Python were used.", True),
            ("VB.NET", "VB.NET was the exact platform.", True),
            ("VB.NET", "VBXNET was the exact platform.", False),
            ("R", "(R) was the exact identifier.", True),
            ("SRE", "SRE2 was the stored team identifier.", False),
        ]
        for topic, source, expected in cases:
            for field in ("title", "text", "fact"):
                with self.subTest(topic=topic, source=source, field=field):
                    profile = {"facts": [{"id": "linked", "text": "Reviewed example."}]}
                    story = {"title": "Reviewed example", "text": "Reviewed example.",
                             "fact_ids": ["linked"]}
                    if field == "fact":
                        profile["facts"][0]["text"] = source
                    else:
                        story[field] = source
                    self.assertEqual(_has_topic_evidence(topic, profile, [story]), expected)

    def test_phrases_cannot_be_assembled_across_stories_or_linked_facts(self):
        profile = {"facts": [
            {"id": "one", "text": "incident"},
            {"id": "two", "text": "response"},
            {"id": "unlinked", "text": "incident response"},
        ]}
        stories = [
            {"title": "incident", "text": "Reviewed example.", "fact_ids": ["one"]},
            {"title": "Reviewed example", "text": "response", "fact_ids": ["two"]},
        ]
        self.assertFalse(_has_topic_evidence("incident response", profile, stories))
        stories[0]["fact_ids"] = ["one", "two"]
        self.assertFalse(_has_topic_evidence("incident response", profile, stories))
        stories[0]["fact_ids"].append("unlinked")
        self.assertTrue(_has_topic_evidence("incident response", profile, stories))


class IndependentPersistedPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review991-independent-")
        self.url = f"sqlite:///{(Path(self.temp.name) / 'review.sqlite').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.profile = {
            "id": "review-owner", "name": "Synthetic Reviewer", "email": "reviewer@example.test",
            "answers": [], "facts": [{
                "id": "selected-fact", "kind": "accomplishment", "text": "Used Rust to maintain a service.",
                "source": "Synthetic reviewed record", "verified": True, "user_approved": True,
                "confidence": 100,
            }, {
                "id": "private-fact", "kind": "accomplishment",
                "text": "Unselected private sentinel: Java experience.",
                "source": "Synthetic private record", "verified": True, "user_approved": True,
                "confidence": 100,
            }],
        }
        self.store.save_profile(deepcopy(self.profile))
        other = deepcopy(self.profile)
        other["id"] = "foreign-owner"
        self.store.save_profile(other)
        with self.store.engine.begin() as connection:
            for company_id in ("review-company", "other-company"):
                upsert(connection, m.companies, {
                    "id": company_id, "name": "Synthetic Same Name", "domain": "example.test",
                })
            upsert(connection, m.jobs, {
                "id": "review-job", "company_id": "review-company", "requisition": "review-role",
                "title": "Synthetic Software Role", "description": "A synthetic Rust and Java role.",
                "skills": ["Rust", "Java"], "location": "Remote", "remote_type": "REMOTE",
                "attributes": {},
            })
        story = create_story(self.store, "review-owner", {
            "kind": "STAR_STORY", "title": "Rust service", "text": "Maintained a Rust service.",
            "fact_ids": ["selected-fact"], "tags": [], "allowed_uses": ["INTERVIEW_PREPARATION"],
        })
        self.story = review_story(self.store, "review-owner", story["story_id"], True, "Synthetic review", 1)
        self._client()

    def _client(self):
        app = FastAPI()
        app.include_router(create_interview_preparation_router(self.store))
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _create(self, selected=None):
        return create_interview_prep_pack(
            self.store, "review-owner", "review-job",
            [self.story["story_id"]] if selected is None else selected,
        )

    def _rows(self):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(m.interview_prep_packs).order_by(m.interview_prep_packs.c.pack_version)
            ).mappings()]

    def test_unselected_facts_do_not_leak_or_satisfy_missing_evidence(self):
        with patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Network forbidden")):
            result = self.client.post(
                "/profiles/review-owner/jobs/review-job/interview-prep-packs",
                json={"selected_story_revision_ids": [self.story["story_id"]]},
            )
        self.assertEqual(result.status_code, 200, result.text)
        pack = result.json()
        self.assertEqual([r["topic"] for r in pack["missing_evidence"]], ["Java"])
        self.assertNotIn("Unselected private sentinel", json.dumps(pack))
        self.assertNotIn("private-fact", json.dumps(pack))
        refs = pack["talking_points"][0]["source_refs"]
        self.assertEqual(refs, [f"career_story_revision:{self.story['story_id']}", "career_fact:selected-fact"])
        self.assertEqual(pack["talking_points"][0]["text"], self.story["text"])
        self.assertEqual(self.store.profile("review-owner")["version"], self.story["profile_version"])

    def test_every_changed_content_field_creates_new_immutable_version(self):
        latest = self._create()
        for field in ("questions", "talking_points", "star_prompts", "missing_evidence", "export_text"):
            with self.subTest(field=field):
                # Model previously persisted content from an earlier generator,
                # without changing profile/job/story inputs or patching code.
                altered = "legacy export\n" if field == "export_text" else [{"legacy": field}]
                with self.store.engine.begin() as connection:
                    connection.execute(m.interview_prep_packs.update().where(
                        m.interview_prep_packs.c.id == latest["pack_id"]
                    ).values(**{field: altered}))
                historical = deepcopy(self._rows())
                corrected = self._create()
                self.assertEqual(corrected["pack_version"], latest["pack_version"] + 1)
                self.assertNotEqual(corrected["pack_id"], latest["pack_id"])
                self.assertEqual(self._rows()[:-1], historical)
                self.assertNotEqual(corrected[field], altered)
                with patch("services.career_profile.interview_preparation.now", return_value="2099-01-01T00:00:00Z"):
                    self.assertEqual(self._create(), corrected)
                latest = corrected
        before_reopen = list_interview_prep_packs(self.store, "review-owner", "review-job")
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self._client()
        self.assertEqual(list_interview_prep_packs(self.store, "review-owner", "review-job"), before_reopen)

    def test_revoked_fact_blocks_new_use_but_retains_historical_pack(self):
        old = self._create()
        current = self.store.profile("review-owner")
        changed = deepcopy(self.profile)
        changed["facts"][0]["user_approved"] = False
        self.store.save_profile(changed, expected_version=current["version"])
        before = self._rows()
        with self.assertRaises(InterviewPreparationError) as raised:
            self._create()
        self.assertEqual(raised.exception.code, "STORY_NOT_SELECTABLE")
        self.assertEqual(self._rows(), before)
        self.assertEqual(get_interview_prep_pack(self.store, "review-owner", "review-job", old["pack_id"]), old)
        empty = self._create([])
        self.assertEqual(empty["talking_points"], [])
        self.assertEqual({r["topic"] for r in empty["missing_evidence"]}, {"Rust", "Java", "Interview examples"})
        self.assertEqual(empty["pack_version"], old["pack_version"] + 1)

    def test_company_identity_change_versions_even_when_display_name_does_not(self):
        old = self._create()
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == "review-job").values(
                company_id="other-company", attributes={"unrelated_label": "synthetic"}
            ))
        changed = self._create()
        self.assertEqual(changed["pack_version"], old["pack_version"] + 1)
        self.assertNotEqual(changed["job_fingerprint"], old["job_fingerprint"])
        self.assertEqual(changed["export_text"], old["export_text"])
        self.assertNotEqual(changed["questions"], old["questions"])
        self.assertEqual(get_interview_prep_pack(self.store, "review-owner", "review-job", old["pack_id"]), old)
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == "review-job").values(
                attributes={"unrelated_label": "changed"}
            ))
        self.assertEqual(self._create(), changed)

    def test_missing_and_foreign_scope_are_sanitized_and_leave_all_state_unchanged(self):
        old = self._create()
        with self.store.engine.connect() as connection:
            before = self.store._workspace_snapshot(connection)
        cases = [
            ("missing-owner", "review-job", [self.story["story_id"]], 404, "PROFILE_NOT_FOUND"),
            ("review-owner", "missing-job", [self.story["story_id"]], 404, "JOB_NOT_FOUND"),
            ("foreign-owner", "review-job", [self.story["story_id"]], 422, "STORY_NOT_SELECTABLE"),
        ]
        for profile_id, job_id, selected, status, code in cases:
            with self.subTest(profile_id=profile_id, job_id=job_id):
                response = self.client.post(
                    f"/profiles/{profile_id}/jobs/{job_id}/interview-prep-packs",
                    json={"selected_story_revision_ids": selected},
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"detail": {"code": "INTERVIEW_PREPARATION_" + code}})
                with self.store.engine.connect() as connection:
                    self.assertEqual(self.store._workspace_snapshot(connection), before)
        foreign_get = self.client.get(
            f"/profiles/foreign-owner/jobs/review-job/interview-prep-packs/{old['pack_id']}"
        )
        self.assertEqual(foreign_get.status_code, 404)
        self.assertNotIn(self.story["text"], foreign_get.text)


if __name__ == "__main__":
    unittest.main()
