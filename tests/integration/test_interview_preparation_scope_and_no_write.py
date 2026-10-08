"""Interview-prep rejects mismatched scope without disturbing durable state."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.interview_preparation import create_interview_preparation_router
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.career_profile.story_bank import create_story, review_story
from tests.integration.test_spine import PROFILE


class InterviewPreparationScopeAndNoWriteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-interview-prep-scope-")
        self.database = (Path(self.directory.name) / "interview-prep.sqlite").as_posix()
        self.store = Store(f"sqlite:///{self.database}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = deepcopy(PROFILE)
        profile["facts"] = [{
            "id": "prep-fact-python",
            "text": "Maintained synthetic Python support services for four test teams.",
            "kind": "accomplishment",
            "source": "Synthetic fixture work history",
            "verified": True,
            "user_approved": True,
            "confidence": 100,
        }]
        self.store.save_profile(profile)
        other = deepcopy(profile)
        other.update({"id": "other", "name": "Other Synthetic Profile"})
        self.store.save_profile(other)
        self.job_id = "scope-job"
        self.other_job_id = "other-scope-job"
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "scope-company", "name": "Synthetic Systems", "domain": "example.test",
            })
            for job_id, requisition in ((self.job_id, "scope-role"),
                                        (self.other_job_id, "other-scope-role")):
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": "scope-company",
                    "requisition": requisition, "title": "Python Support Engineer",
                    "description": "Synthetic job requiring Python incident response.",
                    "skills": ["Python", "incident response"], "location": "Remote",
                    "remote_type": "REMOTE", "attributes": {},
                })
            upsert(connection, m.routes, {
                "id": "scope-route", "job_id": self.job_id,
                "company_id": "scope-company", "provider": "fixture",
                "tenant": "scope", "requisition": "scope-role",
                "url": "https://jobs.example.test/scope",
            })
            upsert(connection, m.applications, {
                "id": "scope-application", "job_id": self.job_id,
                "route_id": "scope-route", "profile_id": "fixture",
                "profile_version": self.store.profile("fixture")["version"],
                "state": "PREPARED", "target": "LOCAL", "fact_ids": [], "trace": {},
                "document_version": 1, "confirmation": None,
                "created_at": "2026-10-09T10:00:00Z", "submitted_at": None,
            })
            upsert(connection, m.documents, {
                "id": "scope-document", "application_id": "scope-application",
                "kind": "RESUME", "version": 1,
                "profile_version": self.store.profile("fixture")["version"],
                "files": {}, "fact_ids": ["prep-fact-python"], "sha256": {},
                "template": "fixture", "created_at": "2026-10-09T10:00:00Z",
                "generation_model": None, "generation_context": {},
            })

        self.fixture_story = self._approved_story("fixture", "Fixture-owned interview story")
        self.other_story = self._approved_story("other", "Other-owned interview story")
        app = FastAPI()
        app.include_router(create_interview_preparation_router(self.store))
        self.client = TestClient(app)
        self.collection = f"/profiles/fixture/jobs/{self.job_id}/interview-prep-packs"

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _approved_story(self, profile_id, title):
        story = create_story(self.store, profile_id, {
            "kind": "STAR_STORY",
            "title": title,
            "text": f"User-authored synthetic account for {title}.",
            "fact_ids": ["prep-fact-python"],
            "tags": ["python", "support"],
            "allowed_uses": ["INTERVIEW_PREPARATION"],
        })
        return review_story(
            self.store, profile_id, story["story_id"], True, None,
            expected_revision=story["revision"],
        )

    def _snapshot_rows(self, table):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(table).order_by(table.c.id)
            ).mappings()]

    def test_scope_failures_leave_existing_pack_documents_and_applications_unchanged(self):
        created = self.client.post(self.collection, json={
            "selected_story_revision_ids": [self.fixture_story["story_id"]],
        })
        self.assertEqual(created.status_code, 200, created.text)
        original_pack = created.json()
        selected_ids = [self.fixture_story["story_id"]]
        self.assertEqual(original_pack["selected_story_revision_ids"], selected_ids)

        before = {
            "packs": self._snapshot_rows(m.interview_prep_packs),
            "documents": self._snapshot_rows(m.documents),
            "applications": self._snapshot_rows(m.applications),
        }

        cross_profile_story = self.client.post(self.collection, json={
            "selected_story_revision_ids": [self.other_story["story_id"]],
        })
        self.assertEqual(cross_profile_story.status_code, 422, cross_profile_story.text)
        self.assertEqual(
            cross_profile_story.json()["detail"]["code"],
            "INTERVIEW_PREPARATION_STORY_NOT_SELECTABLE",
        )

        cross_profile_job = self.client.post(
            f"/profiles/other/jobs/{self.job_id}/interview-prep-packs",
            json={"selected_story_revision_ids": [self.fixture_story["story_id"]]},
        )
        self.assertEqual(cross_profile_job.status_code, 422, cross_profile_job.text)

        missing_job = self.client.post(
            "/profiles/fixture/jobs/missing-job/interview-prep-packs",
            json={"selected_story_revision_ids": [self.fixture_story["story_id"]]},
        )
        self.assertEqual(missing_job.status_code, 404, missing_job.text)
        mismatched_detail = self.client.get(
            f"/profiles/other/jobs/{self.job_id}/interview-prep-packs/{original_pack['pack_id']}"
        )
        self.assertEqual(mismatched_detail.status_code, 404, mismatched_detail.text)
        wrong_job_detail = self.client.get(
            f"/profiles/fixture/jobs/{self.other_job_id}/interview-prep-packs/{original_pack['pack_id']}"
        )
        self.assertEqual(wrong_job_detail.status_code, 404, wrong_job_detail.text)

        self.assertEqual(self._snapshot_rows(m.interview_prep_packs), before["packs"])
        self.assertEqual(self._snapshot_rows(m.documents), before["documents"])
        self.assertEqual(self._snapshot_rows(m.applications), before["applications"])

        listed = self.client.get(self.collection)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["packs"]), 1)
        self.assertEqual(listed.json()["packs"][0]["selected_story_revision_ids"], selected_ids)
        readback = self.client.get(f"{self.collection}/{original_pack['pack_id']}")
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json()["selected_story_revision_ids"], selected_ids)
        self.assertEqual(self._snapshot_rows(m.interview_prep_packs), before["packs"])
        self.assertEqual(self._snapshot_rows(m.documents), before["documents"])
        self.assertEqual(self._snapshot_rows(m.applications), before["applications"])


if __name__ == "__main__":
    unittest.main()
