"""Acceptance for truthful role-lens generation and durable source provenance."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.career_profile.story_bank import create_story, review_story
from services.documents.errors import PreparationError
from services.documents.generate import prepare
from services.documents.history import application_documents


PROFILE = {
    "id": "fixture",
    "name": "Synthetic Role Lens Applicant",
    "email": "role-lens@example.test",
    "answers": [],
    "facts": [
        {"id": "linux", "kind": "experience",
         "text": "Maintained Linux servers and resolved production incidents.",
         "source": "Synthetic reviewed work history", "verified": True,
         "user_approved": True, "confidence": 100},
        {"id": "cloud", "kind": "project",
         "text": "Built AWS infrastructure with Terraform and configured IAM.",
         "source": "Synthetic reviewed project record", "verified": True,
         "user_approved": True, "confidence": 100},
        {"id": "support", "kind": "accomplishment",
         "text": "Resolved customer support tickets and documented repeat solutions.",
         "source": "Synthetic reviewed support record", "verified": True,
         "user_approved": True, "confidence": 100},
        {"id": "security", "kind": "accomplishment",
         "text": "Audited access controls and patched reported vulnerabilities.",
         "source": "Synthetic reviewed security record", "verified": True,
         "user_approved": True, "confidence": 100},
    ],
}


class RoleLensGenerationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-role-lens-generation-")
        self.root = Path(self.directory.name)
        self.database = self.root / "role-lens.sqlite"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.job_id = self._seed_job()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _seed_job(self):
        job_id = "role-lens-job"
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "role-lens-company", "name": "Synthetic Systems",
                "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": job_id, "company_id": "role-lens-company",
                "requisition": job_id, "title": "Systems Administrator",
                "description": (
                    "Responsibilities:\n- Administer Linux systems.\n"
                    "- Build cloud infrastructure.\n- Resolve support incidents."
                ),
                "skills": ["Linux", "AWS", "Python"],
                "location": "Remote", "remote_type": "REMOTE",
                "attributes": {"required_skills": ["Linux", "AWS"]},
            })
            upsert(connection, m.routes, {
                "id": "role-lens-route", "job_id": job_id,
                "company_id": "role-lens-company", "provider": "fixture",
                "tenant": "role-lens", "requisition": job_id,
                "url": "https://jobs.example.test/role-lens",
            })
        return job_id

    def _create_story(self, title, text, fact_id, *, approve=True):
        story = create_story(self.store, "fixture", {
            "kind": "STAR_STORY", "title": title, "text": text,
            "fact_ids": [fact_id], "tags": ["role-lens"],
            "allowed_uses": ["RESUME", "COVER_LETTER"],
        })
        if approve:
            story = review_story(
                self.store, "fixture", story["story_id"], True, None,
                expected_revision=story["revision"],
            )
        return story

    def _seed_story_review_states(self):
        stale = self._create_story(
            "Stale Linux story",
            "I administered a Linux fleet under the previous evidence revision.",
            "linux",
        )
        current = self.store.profile("fixture")
        changed = {
            **deepcopy(PROFILE),
            "id": "fixture",
            "name": current["name"],
            "email": current["email"],
            "facts": [
                {**fact, "text": "Maintained Linux hosts and resolved production incidents."}
                if fact["id"] == "linux" else fact
                for fact in PROFILE["facts"]
            ],
            "expected_version": current["version"],
        }
        self.store.save_profile(changed, expected_version=current["version"])
        approved = self._create_story(
            "Current cloud story",
            "I built a synthetic AWS environment with Terraform and reviewed access controls.",
            "cloud",
        )
        pending = self._create_story(
            "Pending support story",
            "I improved a synthetic support escalation process.",
            "support", approve=False,
        )
        return stale, approved, pending

    def _prepare(self, lens_id=None, lens_version=None):
        options = {}
        if lens_id is not None:
            options["role_lens_id"] = lens_id
        if lens_version is not None:
            options["expected_role_lens_version"] = lens_version
        return prepare(self.store, self.job_id, "fixture", self.root, **options)

    def test_explicit_lenses_change_emphasis_without_changing_facts_and_keep_sources(self):
        stale, approved, pending = self._seed_story_review_states()
        profile_before = self.store.profile("fixture")
        approved_fact_ids = {
            fact.get("fact_id", fact["id"]) for fact in profile_before["facts"]
            if fact["verified"] and fact["user_approved"] and fact["confidence"] >= 80
        }

        systems_application = self._prepare("systems", 1)
        systems_documents = application_documents(self.store, systems_application, self.root)
        systems_resume = next(row for row in systems_documents if row["kind"] == "resume")
        systems_lens = systems_resume["generation_context"]["role_lens"]

        cloud_application = self._prepare("cloud", 1)
        cloud_documents = application_documents(self.store, cloud_application, self.root)
        cloud_resume = next(row for row in cloud_documents
                            if row["kind"] == "resume" and row["version"] > systems_resume["version"])
        cloud_lens = cloud_resume["generation_context"]["role_lens"]

        self.assertEqual(systems_lens["id"], "systems")
        self.assertEqual(cloud_lens["id"], "cloud")
        self.assertEqual(systems_lens["version"], 1)
        self.assertEqual(cloud_lens["version"], 1)
        self.assertNotEqual(systems_lens["emphasized_fact_ids"], cloud_lens["emphasized_fact_ids"])
        self.assertTrue(set(systems_lens["fact_ids"]).issubset(approved_fact_ids))
        self.assertTrue(set(cloud_lens["fact_ids"]).issubset(approved_fact_ids))
        self.assertEqual(self.store.profile("fixture"), profile_before)

        for document_set in (systems_documents, cloud_documents):
            for document in document_set:
                lens = document["generation_context"]["role_lens"]
                references = {item["fact_id"] for item in lens["source_refs"]["facts"]}
                self.assertTrue(set(document["fact_ids"]).issubset(references))
                self.assertTrue(all(item["fingerprint"] for item in lens["source_refs"]["facts"]))
                story_refs = lens["source_refs"]["stories"]
                story_ids = {item["story_id"] for item in story_refs}
                self.assertNotIn(stale["story_id"], story_ids)
                self.assertNotIn(pending["story_id"], story_ids)

        cloud_story_refs = cloud_lens["source_refs"]["stories"]
        current_story_ref = next(item for item in cloud_story_refs
                                 if item["story_id"] == approved["story_id"])
        self.assertEqual(current_story_ref["revision"], approved["revision"])
        self.assertEqual(current_story_ref["fact_fingerprints"], approved["fact_fingerprints"])

        self.assertEqual(self.store.profile("fixture"), profile_before)

    def test_unknown_malformed_and_stale_lens_versions_fail_without_writes(self):
        before_application_count = self.store.count("applications")
        before_document_count = self.store.count("documents")
        cases = (
            ("not a lens id", 1, "ROLE_LENS_ID_INVALID"),
            ("not_configured", 1, "ROLE_LENS_NOT_FOUND"),
            ("systems", "one", "ROLE_LENS_VERSION_INVALID"),
            ("systems", 999, "ROLE_LENS_VERSION_CONFLICT"),
        )
        for lens_id, version, expected_code in cases:
            with self.subTest(lens_id=lens_id, version=version):
                with self.assertRaises(PreparationError) as raised:
                    self._prepare(lens_id, version)
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(self.store.count("applications"), before_application_count)
                self.assertEqual(self.store.count("documents"), before_document_count)
                self.assertFalse((self.root / identity(self.job_id, "fixture")).exists())

    def test_omitted_lens_preserves_legacy_output_and_records_no_lens_selection(self):
        first = self._prepare()
        first_documents = application_documents(self.store, first, self.root)
        first_resume = next(row for row in first_documents if row["kind"] == "resume")
        first_letter = next(row for row in first_documents if row["kind"] == "cover-letter")
        self.assertNotIn("role_lens", first_resume["generation_context"] or {})
        self.assertNotIn("role_lens", first_letter["generation_context"] or {})

        second = self._prepare()
        second_documents = application_documents(self.store, second, self.root)
        second_resume = next(row for row in second_documents
                             if row["kind"] == "resume" and row["version"] == first_resume["version"])
        second_letter = next(row for row in second_documents
                             if row["kind"] == "cover-letter" and row["version"] == first_letter["version"])
        self.assertEqual(second, first)
        self.assertEqual(second_resume["sha256"], first_resume["sha256"])
        self.assertEqual(second_letter["sha256"], first_letter["sha256"])
        self.assertEqual(second_resume["fact_ids"], first_resume["fact_ids"])
        self.assertEqual(second_letter["fact_ids"], first_letter["fact_ids"])


if __name__ == "__main__":
    unittest.main()
