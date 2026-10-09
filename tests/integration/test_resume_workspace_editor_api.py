"""Connected API acceptance for reviewed preview, durable version history, and text export."""
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from docx import Document
from pypdf import PdfReader
import io

from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.resume_templates import list_resume_templates
from services.documents.tailor import target_fingerprint
from tests.integration.test_spine import PROFILE
from apps.api.resume_workspace_editor import create_resume_workspace_editor_router


class ResumeWorkspaceEditorApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-resume-workspace-editor-api-")
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.temporary.name) / 'workspace.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.save_profile(dict(PROFILE, facts=[
            {"id": "fact-7", "text": "Maintained approved production systems.",
             "kind": "responsibility", "source": "synthetic profile", "verified": True,
             "user_approved": True, "confidence": 100},
            {"id": "fact-8", "text": "Linux administration.", "kind": "skill",
             "source": "synthetic profile", "verified": True,
             "user_approved": True, "confidence": 100},
        ]))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job = self.store.jobs("fixture")[0]
        self.job_fingerprint = target_fingerprint(self.store.job(self.job["id"], "fixture"))
        self.profile = self.store.profile("fixture")
        self.template = next(row for row in list_resume_templates()
                             if row["template_id"] == "verified-headings-v1")
        self.sections = [
            {"section_id": "experience", "visible": True, "position": 0, "fact_ids": ["fact-7"]},
            {"section_id": "skill", "visible": True, "position": 1, "fact_ids": ["fact-8"]},
        ]
        app = FastAPI()
        app.include_router(create_resume_workspace_editor_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.path = f"/profiles/fixture/jobs/{self.job['id']}/resume-workspace"
        self.preview_body = {
            "template_id": self.template["template_id"],
            "sections": self.sections,
            "expected_profile_version": self.profile["version"],
            "expected_job_fingerprint": self.job_fingerprint,
            "expected_template_version": self.template["version"],
        }

    def test_reviewed_preview_save_reload_and_text_export_use_one_immutable_snapshot(self):
        before = (self.store.count("resume_workspace_revisions"),
                  self.store.count("documents"), self.store.count("applications"))
        preview = self.client.post(self.path + "/preview", json=self.preview_body)
        self.assertEqual(preview.status_code, 200, preview.text)
        preview_body = preview.json()
        self.assertTrue(preview_body["read_only"])
        self.assertFalse(preview_body["document_generated"])
        self.assertIn("Maintained approved production systems.", preview_body["rendered_text"])
        self.assertEqual((self.store.count("resume_workspace_revisions"),
                          self.store.count("documents"), self.store.count("applications")), before)

        save_body = {
            "expected_version": None,
            "profile_version": self.profile["version"],
            "job_fingerprint": self.job_fingerprint,
            "template_id": self.template["template_id"],
            "sections": self.sections,
            "preview_fingerprint": preview_body["preview_fingerprint"],
            "reviewed": True,
            "request_id": "resume-workspace-save-1",
        }
        saved = self.client.put(self.path, json=save_body)
        self.assertEqual(saved.status_code, 200, saved.text)
        revision = saved.json()["workspace_revision"]
        self.assertEqual(revision["version"], 1)
        self.assertEqual(revision["rendered_text"], preview_body["rendered_text"])
        self.assertEqual(revision["source_refs"], preview_body["source_refs"])

        reloaded = self.client.get(self.path)
        self.assertEqual(reloaded.status_code, 200, reloaded.text)
        self.assertEqual(reloaded.json()["current"], revision)
        export = self.client.get(self.path + "/versions/1/text")
        self.assertEqual(export.status_code, 200, export.text)
        self.assertEqual(export.text, preview_body["rendered_text"])
        self.assertEqual(export.headers["content-type"].split(";")[0], "text/plain")
        docx = self.client.get(self.path + "/versions/1/docx")
        pdf = self.client.get(self.path + "/versions/1/pdf")
        self.assertEqual(docx.status_code, 200, docx.text)
        self.assertEqual(pdf.status_code, 200, pdf.text)
        self.assertEqual(docx.headers["content-type"].split(";")[0],
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertEqual(pdf.headers["content-type"].split(";")[0], "application/pdf")
        expected_paragraphs = preview_body["rendered_text"].splitlines()
        self.assertEqual([p.text for p in Document(io.BytesIO(docx.content)).paragraphs], expected_paragraphs)
        pdf_text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
        self.assertTrue(all(line in pdf_text for line in expected_paragraphs), pdf_text)
        self.assertEqual(self.store.count("documents"), 0)
        self.assertEqual(self.store.count("applications"), 0)

        replay = self.client.put(self.path, json=save_body)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.store.count("resume_workspace_revisions"), 1)
        current_profile = self.store.profile("fixture")
        changed_profile = dict(current_profile)
        changed_profile["preferences"] = {**(current_profile.get("preferences") or {}),
                                         "avoid_terms": ["Maintained approved production systems"]}
        self.store.save_profile(changed_profile, expected_version=current_profile["version"],
                                require_expected_version=True)
        for format in ("txt", "docx", "pdf"):
            blocked = self.client.get(self.path + f"/versions/1/{format}")
            self.assertEqual(blocked.status_code, 409, blocked.text)
            self.assertEqual(blocked.json()["detail"]["code"], "AVOID_LIST_BLOCKED")
            self.assertNotIn("Maintained approved production systems", blocked.text)
        self.assertEqual(self.store.count("resume_workspace_revisions"), 1)
        self.assertEqual(self.store.count("documents"), 0)
        self.assertEqual(self.store.count("applications"), 0)

    def test_unreviewed_stale_and_foreign_requests_fail_without_writes(self):
        preview = self.client.post(self.path + "/preview", json=self.preview_body)
        body = {
            "expected_version": None,
            "profile_version": self.profile["version"],
            "job_fingerprint": self.job_fingerprint,
            "template_id": self.template["template_id"],
            "sections": self.sections,
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "reviewed": False,
            "request_id": "unreviewed-save",
        }
        unreviewed = self.client.put(self.path, json=body)
        self.assertEqual(unreviewed.status_code, 422, unreviewed.text)
        stale = self.client.put(self.path, json={**body, "reviewed": True,
            "preview_fingerprint": "0" * 64, "request_id": "stale-save"})
        self.assertEqual(stale.status_code, 409, stale.text)
        foreign = self.client.get(f"/profiles/other/jobs/{self.job['id']}/resume-workspace/versions/1/text")
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(self.store.count("resume_workspace_revisions"), 0)
        self.assertEqual(self.store.count("documents"), 0)
        self.assertEqual(self.store.count("applications"), 0)


if __name__ == "__main__":
    unittest.main()
