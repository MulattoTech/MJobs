"""Durable, immutable job-scoped resume workspace revisions."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.resume_workspace import (
    list_resume_workspace_revisions,
    save_resume_workspace_revision,
)
from services.documents.tailor import target_fingerprint
from services.documents.templates import BASE_TEMPLATE
from tests.integration.test_spine import PROFILE


class ResumeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-resume-workspace-")
        self.root = Path(self.temporary.name)
        self.store = Store(f"sqlite:///{(self.root / 'workspace.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile(dict(PROFILE, id="other", email="other@example.test"))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.jobs = self.store.jobs("fixture")
        self.job = self.jobs[0]
        self.profile = self.store.profile("fixture")
        self.job_fingerprint = target_fingerprint(self.store.job(self.job["id"], "fixture"))
        self.sections = [
            {"section_id": "experience", "visible": True, "position": 0,
             "fact_ids": ["python"]},
            {"section_id": "skill", "visible": False, "position": 1,
             "fact_ids": ["sql"]},
        ]

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def _save(self, *, profile_id="fixture", job_id=None, expected_version=None,
              sections=None, profile_version=None, job_fingerprint=None,
              template_id=BASE_TEMPLATE):
        if profile_version is None:
            profile_version = self.store.profile(profile_id)["version"]
        return save_resume_workspace_revision(
            self.store,
            profile_id=profile_id,
            job_id=job_id or self.job["id"],
            expected_version=expected_version,
            profile_version=profile_version,
            job_fingerprint=job_fingerprint or self.job_fingerprint,
            template_id=template_id,
            sections=self.sections if sections is None else sections,
        )

    def test_save_reads_back_exact_order_template_visibility_and_fact_references(self):
        saved = self._save()

        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["profile_id"], "fixture")
        self.assertEqual(saved["job_id"], self.job["id"])
        self.assertEqual(saved["profile_version"], self.profile["version"])
        self.assertEqual(saved["job_fingerprint"], self.job_fingerprint)
        self.assertEqual(saved["template_id"], BASE_TEMPLATE)
        self.assertEqual(saved["sections"], self.sections)
        self.assertIsNotNone(saved["created_at"])
        self.assertEqual(list_resume_workspace_revisions(
            self.store, "fixture", self.job["id"]), [saved])

    def test_stale_expected_version_fails_without_adding_a_revision(self):
        first = self._save()
        changed = [dict(self.sections[0], visible=False), self.sections[1]]

        with self.assertRaises(ValueError) as raised:
            self._save(expected_version=None, sections=changed)

        self.assertEqual(getattr(raised.exception, "code", None),
                         "RESUME_WORKSPACE_STALE_VERSION")
        self.assertEqual([row["id"] for row in list_resume_workspace_revisions(
            self.store, "fixture", self.job["id"])], [first["id"]])

    def test_profile_and_job_pairs_are_isolated(self):
        other_job = self.jobs[1]
        self.assertEqual(list_resume_workspace_revisions(
            self.store, "other", self.job["id"]), [])
        self.assertEqual(list_resume_workspace_revisions(
            self.store, "fixture", other_job["id"]), [])

        original = self._save()
        other_profile = self._save(profile_id="other")
        other_job_row = self._save(job_id=other_job["id"],
                                   job_fingerprint=target_fingerprint(
                                       self.store.job(other_job["id"], "fixture")))

        self.assertEqual(list_resume_workspace_revisions(
            self.store, "fixture", self.job["id"]), [original])
        self.assertEqual(list_resume_workspace_revisions(
            self.store, "other", self.job["id"]), [other_profile])
        self.assertEqual(list_resume_workspace_revisions(
            self.store, "fixture", other_job["id"]), [other_job_row])

    def test_identical_retry_returns_the_same_revision_without_duplicate(self):
        first = self._save()
        replay = self._save(expected_version=None)

        self.assertEqual(replay, first)
        self.assertEqual(self.store.count("resume_workspace_revisions"), 1)

    def test_workspace_save_does_not_mutate_canonical_profile_facts(self):
        before_profile = self.store.profile("fixture")
        with self.store.engine.connect() as connection:
            before_facts = [dict(row) for row in connection.execute(
                select(m.facts).where(m.facts.c.profile_id == "fixture").order_by(m.facts.c.id)
            ).mappings()]

        self._save()

        after_profile = self.store.profile("fixture")
        with self.store.engine.connect() as connection:
            after_facts = [dict(row) for row in connection.execute(
                select(m.facts).where(m.facts.c.profile_id == "fixture").order_by(m.facts.c.id)
            ).mappings()]
        self.assertEqual(after_profile["facts"], before_profile["facts"])
        self.assertEqual(after_facts, before_facts)

    def test_malformed_sections_and_unknown_templates_fail_closed(self):
        with self.assertRaises(ValueError) as malformed:
            self._save(sections=[{"section_id": "unknown", "visible": True,
                                  "position": 0, "fact_ids": ["python"]}])
        self.assertEqual(getattr(malformed.exception, "code", None),
                         "RESUME_WORKSPACE_INVALID")

        with self.assertRaises(ValueError) as template:
            self._save(template_id="invented-template")
        self.assertEqual(getattr(template.exception, "code", None),
                         "RESUME_WORKSPACE_INVALID")


if __name__ == "__main__":
    unittest.main()
