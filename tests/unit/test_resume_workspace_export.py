"""Reviewed resume workspace versions preserve the exact rendered source snapshot."""
from pathlib import Path
import tempfile
import unittest

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.resume_templates import list_resume_templates
from services.documents.resume_workspace_export import save_reviewed_resume_workspace_export
from services.documents.tailor import target_fingerprint
from tests.integration.test_spine import PROFILE


class ResumeWorkspaceExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-reviewed-resume-workspace-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(f"sqlite:///{(self.root / 'workspace.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.save_profile(dict(PROFILE, facts=[
            {"id": "fact-python", "text": "Maintained Python service reliability.",
             "kind": "responsibility", "source": "synthetic profile", "verified": True,
             "user_approved": True, "confidence": 100},
        ]))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job = self.store.jobs("fixture")[0]
        self.profile = self.store.profile("fixture")
        self.job_fingerprint = target_fingerprint(self.store.job(self.job["id"], "fixture"))
        self.template = next(row for row in list_resume_templates()
                             if row["template_id"] == "verified-headings-v1")
        self.sections = [{
            "section_id": "experience", "visible": True, "position": 0,
            "fact_ids": ["fact-python"],
        }]

    def _save(self, **overrides):
        values = {
            "store": self.store,
            "profile_id": "fixture",
            "job_id": self.job["id"],
            "expected_version": None,
            "profile_version": self.profile["version"],
            "job_fingerprint": self.job_fingerprint,
            "template_id": self.template["template_id"],
            "sections": self.sections,
            "preview_fingerprint": "",
            "reviewed": True,
            "request_id": "resume-save-1",
        }
        values.update(overrides)
        return save_reviewed_resume_workspace_export(**values)

    def test_save_recomputes_and_persists_exact_reviewed_preview_for_reload_and_export(self):
        from services.documents.resume_workspace_preview import build_resume_workspace_draft_preview

        preview = build_resume_workspace_draft_preview(
            self.store, "fixture", self.job["id"],
            template_id=self.template["template_id"], sections=self.sections,
            expected_profile_version=self.profile["version"],
            expected_job_fingerprint=self.job_fingerprint,
            expected_template_version=self.template["version"],
        )

        saved = self._save(preview_fingerprint=preview["preview_fingerprint"])

        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["rendered_text"], preview["rendered_text"])
        self.assertEqual(saved["source_refs"], preview["source_refs"])
        self.assertEqual(saved["preview_fingerprint"], preview["preview_fingerprint"])
        self.assertIn("Maintained Python service reliability.", saved["rendered_text"])
        self.assertTrue(saved["created_at"])
        self.assertEqual(self.store.count("resume_workspace_revisions"), 1)
        self.assertEqual(self.store.count("documents"), 0)
        self.assertEqual(self.store.count("applications"), 0)

    def test_same_request_replays_only_identical_payload_and_changed_payload_conflicts(self):
        from services.documents.resume_workspace_preview import build_resume_workspace_draft_preview

        preview = build_resume_workspace_draft_preview(
            self.store, "fixture", self.job["id"],
            template_id=self.template["template_id"], sections=self.sections,
            expected_profile_version=self.profile["version"],
            expected_job_fingerprint=self.job_fingerprint,
            expected_template_version=self.template["version"],
        )
        first = self._save(preview_fingerprint=preview["preview_fingerprint"])
        replay = self._save(preview_fingerprint=preview["preview_fingerprint"])

        self.assertEqual(replay["id"], first["id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.store.count("resume_workspace_revisions"), 1)
        with self.assertRaises(ValueError) as raised:
            self._save(preview_fingerprint=preview["preview_fingerprint"],
                       sections=[dict(self.sections[0], visible=False)])
        self.assertEqual(getattr(raised.exception, "code", None),
                         "RESUME_WORKSPACE_REQUEST_CONFLICT")
        self.assertEqual(self.store.count("resume_workspace_revisions"), 1)

    def test_unreviewed_and_stale_preview_requests_never_write(self):
        with self.assertRaises(ValueError):
            self._save(reviewed=False)
        with self.assertRaises(ValueError):
            self._save(preview_fingerprint="0" * 64)
        self.assertEqual(self.store.count("resume_workspace_revisions"), 0)


if __name__ == "__main__":
    unittest.main()
