"""Deterministic text-only previews of saved resume workspace revisions."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.resume_templates import list_resume_templates
from services.documents.resume_workspace import save_resume_workspace_revision
from services.documents.resume_workspace_preview import build_resume_workspace_preview
from services.documents.tailor import source_fact_fingerprint, target_fingerprint
from tests.integration.test_spine import PROFILE


class ResumeWorkspacePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-resume-workspace-preview-")
        self.root = Path(self.temporary.name)
        self.store = Store(f"sqlite:///{(self.root / 'preview.db').as_posix()}")
        self.store.migrate()
        self.profile_input = dict(PROFILE, facts=[
            {"id": "python", "text": "Python platform support", "kind": "skill",
             "source": "fictional-test-profile", "verified": True, "user_approved": True,
             "confidence": 100},
            {"id": "duty", "text": "Administer Linux servers and restore production services.",
             "kind": "responsibility", "source": "fictional-test-profile", "verified": True,
             "user_approved": True, "confidence": 100},
            {"id": "hidden", "text": "Kubernetes platform work", "kind": "skill",
             "source": "fictional-test-profile", "verified": True, "user_approved": True,
             "confidence": 100},
        ])
        self.store.save_profile(self.profile_input)
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
            {"section_id": "project", "visible": False, "position": 2,
             "fact_ids": ["hidden"]},
        ]
        self.workspace = save_resume_workspace_revision(
            self.store, profile_id="fixture", job_id=self.job["id"],
            expected_version=None, profile_version=self.profile["version"],
            job_fingerprint=self.job_fingerprint,
            template_id=self.template["template_id"], sections=self.sections,
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def _preview(self, **overrides):
        arguments = {
            "workspace_version": self.workspace["version"],
            "expected_profile_version": self.profile["version"],
            "expected_job_fingerprint": self.job_fingerprint,
            "expected_template_version": self.template["version"],
        }
        arguments.update(overrides)
        return build_resume_workspace_preview(
            self.store, "fixture", self.job["id"], **arguments)

    def test_preview_renders_exact_visible_fact_text_in_saved_section_order(self):
        result = self._preview()

        self.assertEqual(result["profile_id"], "fixture")
        self.assertEqual(result["job_id"], self.job["id"])
        self.assertEqual(result["workspace_version"], self.workspace["version"])
        self.assertEqual(result["template"]["template_id"], self.template["template_id"])
        self.assertEqual(result["template"]["version"], self.template["version"])
        self.assertEqual(result["sections"], self.sections)
        self.assertEqual([row["section_id"] for row in result["rendered_sections"]],
                         ["skill", "experience"])
        self.assertEqual([row["fact_ids"] for row in result["rendered_sections"]],
                         [["python"], ["duty"]])
        self.assertEqual([row["text"] for row in result["rendered_sections"]], [
            "Python platform support",
            "Administer Linux servers and restore production services.",
        ])
        self.assertEqual(result["preview_text"], "\n".join([
            self.profile["name"], self.profile["email"], "Skills", "Python platform support",
            "Experience", "Administer Linux servers and restore production services.",
        ]))
        self.assertNotIn("Kubernetes platform work", result["preview_text"])
        self.assertTrue(result["read_only"])
        self.assertTrue(result["text_only"])
        self.assertTrue(result["ats_safe"])
        self.assertFalse(result["document_generated"])

    def test_preview_preserves_visible_fact_provenance_and_alignment_is_analysis_only(self):
        before = (self.store.count("applications"), self.store.count("documents"))
        result = self._preview()
        facts_by_id = {row["fact_id"]: row for row in self.profile["facts"]}

        self.assertEqual(result["source_refs"]["facts"], [
            {"fact_id": "python", "source": "fictional-test-profile", "kind": "skill",
             "profile_version": self.profile["version"],
             "fact_fingerprint": source_fact_fingerprint(facts_by_id["python"])},
            {"fact_id": "duty", "source": "fictional-test-profile", "kind": "responsibility",
             "profile_version": self.profile["version"],
             "fact_fingerprint": source_fact_fingerprint(facts_by_id["duty"])},
        ])
        self.assertEqual(result["keyword_alignment"]["selected_fact_ids"], ["python", "duty"])
        self.assertEqual(result["keyword_alignment"]["matches"]["skills"][0],
                         {"term": "Python", "fact_ids": ["python"]})
        self.assertIn("Kubernetes", result["keyword_alignment"]["unmatched"]["skills"])
        self.assertTrue(result["read_only"])
        self.assertEqual((self.store.count("applications"), self.store.count("documents")), before)

    def test_stale_profile_job_workspace_and_template_snapshots_fail_with_stable_codes(self):
        stale_cases = [
            ({"expected_profile_version": "0" * 64}, "RESUME_WORKSPACE_PROFILE_STALE"),
            ({"expected_job_fingerprint": "0" * 64}, "RESUME_WORKSPACE_JOB_STALE"),
            ({"workspace_version": 99}, "RESUME_WORKSPACE_STALE_CONTEXT"),
            ({"expected_template_version": "previous"}, "RESUME_TEMPLATE_VERSION_STALE"),
        ]
        for overrides, expected_code in stale_cases:
            with self.subTest(expected_code=expected_code), self.assertRaises(ValueError) as raised:
                self._preview(**overrides)
            self.assertEqual(getattr(raised.exception, "code", None), expected_code)

    def test_stored_snapshot_mismatch_is_rejected_after_profile_changes(self):
        changed = dict(self.profile_input, facts=[
            dict(self.profile_input["facts"][0], text="Changed Python fact"),
            *self.profile_input["facts"][1:],
        ])
        self.store.save_profile(changed, expected_version=self.profile["version"],
                                require_expected_version=True)
        current_profile = self.store.profile("fixture")

        with self.assertRaises(ValueError) as raised:
            self._preview(expected_profile_version=current_profile["version"])

        self.assertEqual(getattr(raised.exception, "code", None),
                         "RESUME_WORKSPACE_STALE_CONTEXT")

    def test_stored_snapshot_mismatch_is_rejected_after_job_changes(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.job["id"]).values(
                skills=["Python", "Terraform"],
            ))
        current_job = self.store.job(self.job["id"], "fixture")

        with self.assertRaises(ValueError) as raised:
            self._preview(expected_job_fingerprint=target_fingerprint(current_job))

        self.assertEqual(getattr(raised.exception, "code", None),
                         "RESUME_WORKSPACE_STALE_CONTEXT")


if __name__ == "__main__":
    unittest.main()
