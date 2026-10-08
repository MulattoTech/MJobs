"""Role-lens document preparation uses only current approved evidence."""
from pathlib import Path
import tempfile
import unittest

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.career_profile.story_bank import create_story, review_story
from services.documents.errors import PreparationError
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


class RoleLensDocumentGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-role-lens-docs-")
        self.root = Path(self.temporary.name)
        self.store = Store(f"sqlite:///{(self.root / 'role-lens.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = {**PROFILE, "facts": [
            *PROFILE["facts"],
            {"id": "windows", "text": "Windows Server and Active Directory administration",
             "kind": "skill", "source": "fictional-test-profile", "verified": True,
             "user_approved": True, "confidence": 100},
            {"id": "aws", "text": "AWS cloud migration and Terraform automation",
             "kind": "skill", "source": "fictional-test-profile", "verified": True,
             "user_approved": True, "confidence": 100},
        ]}
        self.store.save_profile(profile)

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def _job(self, origin):
        discover(self.store, origin, "fixture")
        return next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")

    def _approved_story(self, title, text, fact_id, tags):
        story = create_story(self.store, "fixture", {
            "kind": "STAR_STORY", "title": title, "text": text,
            "fact_ids": [fact_id], "tags": tags,
            "allowed_uses": ["RESUME", "COVER_LETTER"],
        })
        review_story(self.store, "fixture", story["story_id"], True, "Reviewed for reuse",
                     story["revision"])

    def test_omitted_lens_keeps_current_fact_order_and_has_no_lens_metadata(self):
        with fixture_server() as origin:
            job = self._job(origin)
            application_id = prepare(self.store, job["id"], "fixture", self.root)

        docs = application_documents(self.store, application_id, self.root)
        resume = next(row for row in docs if row["kind"] == "resume")
        letter = next(row for row in docs if row["kind"] == "cover-letter")
        self.assertEqual(resume["fact_ids"], [row["id"] for row in PROFILE["facts"]]
                         + ["windows", "aws"])
        self.assertNotIn("role_lens", resume.get("generation_context") or {})
        self.assertNotIn("role_lens", letter.get("generation_context") or {})

    def test_explicit_lens_reorders_and_emphasizes_existing_facts_with_sources(self):
        self._approved_story(
            "Improved server reliability",
            "Situation: service reliability needed improvement. Task: reduce disruption. "
            "Action: automated Windows Server health checks. Result: incidents fell.",
            "windows", ["systems", "reliability"],
        )
        self._approved_story(
            "Automated cloud deployments",
            "Situation: deployments were slow. Task: improve cloud delivery. "
            "Action: automated AWS Terraform workflows. Result: releases became repeatable.",
            "aws", ["cloud", "automation"],
        )
        with fixture_server() as origin:
            job = self._job(origin)
            application_id = prepare(
                self.store, job["id"], "fixture", self.root,
                role_lens_id="systems", expected_role_lens_version=1,
            )
            same_application = prepare(
                self.store, job["id"], "fixture", self.root,
                role_lens_id="cloud", expected_role_lens_version=1,
            )

        docs = application_documents(self.store, application_id, self.root)
        self.assertEqual(same_application, application_id)
        system_resume = next(row for row in docs if row["kind"] == "resume" and row["version"] == 1)
        cloud_resume = next(row for row in docs if row["kind"] == "resume" and row["version"] == 2)
        system_letter = next(row for row in docs if row["kind"] == "cover-letter" and row["version"] == 1)
        cloud_letter = next(row for row in docs if row["kind"] == "cover-letter" and row["version"] == 2)
        systems = system_resume["generation_context"]["role_lens"]
        cloud = cloud_resume["generation_context"]["role_lens"]
        self.assertEqual((systems["id"], systems["version"]), ("systems", 1))
        self.assertEqual((cloud["id"], cloud["version"]), ("cloud", 1))
        self.assertEqual(set(system_resume["fact_ids"]), set(cloud_resume["fact_ids"]))
        self.assertEqual(set(system_resume["fact_ids"]), {row["id"] for row in PROFILE["facts"]}
                         | {"windows", "aws"})
        self.assertNotEqual(system_resume["fact_ids"], cloud_resume["fact_ids"])
        self.assertEqual(systems["document_fact_ids"], system_resume["fact_ids"])
        self.assertEqual(cloud["document_fact_ids"], cloud_resume["fact_ids"])
        self.assertIn("windows", systems["emphasized_fact_ids"])
        self.assertIn("aws", cloud["emphasized_fact_ids"])
        self.assertEqual(systems["document_emphasized_fact_ids"],
                         system_resume["generation_context"]["emphasized_fact_ids"])
        self.assertEqual(cloud["document_emphasized_fact_ids"],
                         cloud_resume["generation_context"]["emphasized_fact_ids"])
        self.assertNotEqual(systems["emphasized_fact_ids"], cloud["emphasized_fact_ids"])
        self.assertNotEqual(systems["emphasized_story_ids"], cloud["emphasized_story_ids"])
        self.assertEqual(
            {row["fact_id"] for row in systems["source_refs"]["facts"]},
            set(systems["fact_ids"]),
        )
        self.assertTrue(all(row["fingerprint"] for row in systems["source_refs"]["facts"]))
        self.assertEqual(
            [row["story_id"] for row in systems["source_refs"]["stories"]],
            systems["story_ids"],
        )
        self.assertEqual(system_letter["generation_context"]["role_lens"]["source_refs"],
                         systems["source_refs"])
        self.assertEqual(cloud_letter["generation_context"]["role_lens"]["source_refs"],
                         cloud["source_refs"])

    def test_stale_lens_version_fails_before_any_document_or_artifact_write(self):
        with fixture_server() as origin:
            job = self._job(origin)
            with self.assertRaises(PreparationError) as raised:
                prepare(self.store, job["id"], "fixture", self.root,
                        role_lens_id="systems", expected_role_lens_version=999)

        self.assertIn("ROLE_LENS", raised.exception.code)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(m.applications.select()).fetchall(), [])
            self.assertEqual(connection.execute(m.documents.select()).fetchall(), [])
        self.assertEqual(list(self.root.glob("*/*")), [])


if __name__ == "__main__":
    unittest.main()
