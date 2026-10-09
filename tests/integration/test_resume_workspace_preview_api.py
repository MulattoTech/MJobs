"""Scoped, read-only API coverage for resume workspace previews."""
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.resume_workspace import resume_workspace_revisions
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.resume_templates import list_resume_templates
from services.documents.resume_workspace import save_resume_workspace_revision
from services.documents.resume_workspace_preview import build_resume_workspace_preview
from services.documents.tailor import target_fingerprint
from tests.integration.test_spine import PROFILE
from apps.api.resume_workspace_preview import create_resume_workspace_preview_router


class ResumeWorkspacePreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-resume-workspace-preview-api-")
        self.root = Path(self.temporary.name)
        self.store = Store(f"sqlite:///{(self.root / 'preview-api.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile(dict(PROFILE, facts=[
            {"id": "python", "text": "Python platform support", "kind": "skill",
             "source": "fictional-test-profile", "verified": True, "user_approved": True,
             "confidence": 100},
            {"id": "duty", "text": "Administer Linux servers and restore production services.",
             "kind": "responsibility", "source": "fictional-test-profile", "verified": True,
             "user_approved": True, "confidence": 100},
        ]))
        self.store.save_profile(dict(PROFILE, id="other", email="other@example.test", facts=[
            {"id": "foreign", "text": "Foreign profile fact", "kind": "skill",
             "source": "fictional-other-profile", "verified": True, "user_approved": True,
             "confidence": 100},
        ]))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job = self.store.jobs("fixture")[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.job["id"]).values(
                skills=["Python", "Kubernetes"],
                description=("Responsibilities:\n"
                             "- Administer Linux servers and restore production services.\n"
                             "- Operate Kubernetes platform workloads at scale."),
            ))
        self.job = self.store.job(self.job["id"], "fixture")
        self.profile = self.store.profile("fixture")
        self.job_fingerprint = target_fingerprint(self.job)
        self.template = next(row for row in list_resume_templates()
                             if row["template_id"] == "verified-headings-v1")
        self.sections = [
            {"section_id": "skill", "visible": True, "position": 0,
             "fact_ids": ["python"]},
            {"section_id": "experience", "visible": True, "position": 1,
             "fact_ids": ["duty"]},
        ]
        self.workspace = save_resume_workspace_revision(
            self.store, profile_id="fixture", job_id=self.job["id"],
            expected_version=None, profile_version=self.profile["version"],
            job_fingerprint=self.job_fingerprint,
            template_id=self.template["template_id"], sections=self.sections,
        )
        app = FastAPI()
        app.include_router(create_resume_workspace_preview_router(self.store))
        self.client = TestClient(app)
        self.path = f"/profiles/fixture/jobs/{self.job['id']}/resume-workspace/preview"
        self.payload = {
            "workspace_version": self.workspace["version"],
            "expected_profile_version": self.profile["version"],
            "expected_job_fingerprint": self.job_fingerprint,
            "expected_template_version": self.template["version"],
        }

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def _counts(self):
        with self.store.engine.connect() as connection:
            return tuple(connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one() for table in (
                m.applications, m.documents, m.operations, resume_workspace_revisions,
            ))

    def _state(self):
        with self.store.engine.connect() as connection:
            workspaces = [dict(row) for row in connection.execute(
                select(resume_workspace_revisions).order_by(resume_workspace_revisions.c.id)
            ).mappings()]
        return self._counts(), workspaces

    def test_preview_returns_exact_service_contract_without_writes(self):
        before = self._state()
        expected = build_resume_workspace_preview(
            self.store, "fixture", self.job["id"], **self.payload)

        response = self.client.post(self.path, json=self.payload)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), expected)
        self.assertEqual(response.json()["profile_id"], "fixture")
        self.assertEqual(response.json()["job_id"], self.job["id"])
        self.assertTrue(response.json()["read_only"])
        self.assertFalse(response.json()["document_generated"])
        self.assertEqual(self._state(), before)

    def test_foreign_profile_cannot_read_another_profiles_workspace_preview(self):
        before = self._state()
        response = self.client.post(
            f"/profiles/other/jobs/{self.job['id']}/resume-workspace/preview",
            json={**self.payload,
                  "expected_profile_version": self.store.profile("other")["version"]},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], {"code": "RESUME_WORKSPACE_STALE_CONTEXT"})
        self.assertNotIn("Python platform support", response.text)
        self.assertEqual(self._state(), before)

    def test_stale_profile_job_workspace_and_template_snapshots_are_stable_conflicts(self):
        cases = [
            ({"expected_profile_version": "0" * 64}, "RESUME_WORKSPACE_PROFILE_STALE"),
            ({"expected_job_fingerprint": "0" * 64}, "RESUME_WORKSPACE_JOB_STALE"),
            ({"workspace_version": 99}, "RESUME_WORKSPACE_STALE_CONTEXT"),
            ({"expected_template_version": "old"}, "RESUME_TEMPLATE_VERSION_STALE"),
        ]
        before = self._state()
        for overrides, code in cases:
            with self.subTest(code=code):
                response = self.client.post(self.path, json={**self.payload, **overrides})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"], {"code": code})
        self.assertEqual(self._state(), before)

    def test_unsupported_saved_template_and_malformed_request_fail_closed(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(resume_workspace_revisions).where(
                resume_workspace_revisions.c.id == self.workspace["id"]
            ).values(template_id="unsupported-template"))
        before = self._state()
        unsupported = self.client.post(self.path, json=self.payload)
        self.assertEqual(unsupported.status_code, 422, unsupported.text)
        self.assertEqual(unsupported.json()["detail"], {"code": "RESUME_TEMPLATE_SELECTION_INVALID"})

        malformed = self.client.post(self.path, json={**self.payload, "extra": True})
        self.assertEqual(malformed.status_code, 422, malformed.text)
        self.assertEqual(self._state(), before)


if __name__ == "__main__":
    unittest.main()
