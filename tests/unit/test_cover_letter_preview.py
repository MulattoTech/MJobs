"""Tests for read-only cover-letter preview and evidence provenance."""
import copy
import hashlib
import unittest

from services.documents.cover_letter_preview import (
    build_cover_letter_preview,
    cover_letter_preview_fingerprint,
)
from services.documents.errors import PreparationError
from services.documents.tailor import source_fact_fingerprint


class CoverLetterPreviewTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "id": "profile-fixture",
            "version": "a" * 64,
            "name": "Alex Example",
            "facts": [
                {"fact_id": "python", "kind": "skill", "text": "Python service development",
                 "source": "reviewed resume", "confidence": 100,
                 "verified": True, "user_approved": True},
                {"fact_id": "duty", "kind": "responsibility",
                 "text": "Built Python services for internal reporting teams.",
                 "source": "reviewed resume", "confidence": 96,
                 "verified": True, "user_approved": True},
                {"fact_id": "pending", "kind": "skill", "text": "Kubernetes administration",
                 "source": "unreviewed import", "confidence": 100,
                 "verified": True, "user_approved": False},
            ],
        }
        self.job = {
            "id": "j" * 64,
            "company_id": "company-fixture",
            "company": "Example Systems",
            "title": "Platform Engineer",
            "skills": ["Python", "SQL"],
            "description": (
                "Responsibilities:\nBuild Python reporting services for internal teams.\n"
                "Maintain SQL data pipelines for monthly reporting.\n"
                "Requirements:\nPython"
            ),
            "attributes": {
                "skill_evidence": "structured_required_skills",
                "required_skills": ["Python"],
                "preferred_skills": ["SQL"],
                "company_identity": "VERIFIED_SOURCE",
            },
        }
        self.profile_version = self.profile["version"]
        self.job_fingerprint = cover_letter_preview_fingerprint(self.job)

    def _build(self, *, profile=None, job=None, facts=None, **options):
        current_profile = self.profile if profile is None else profile
        return build_cover_letter_preview(
            current_profile,
            self.job if job is None else job,
            current_profile.get("facts") if facts is None else facts,
            expected_profile_version=options.pop("expected_profile_version", self.profile_version),
            expected_job_fingerprint=options.pop("expected_job_fingerprint", self.job_fingerprint),
            facts_profile_id=options.pop("facts_profile_id", current_profile.get("id")),
            **options,
        )

    def test_preview_is_deterministic_and_returns_selected_approved_provenance(self):
        before = copy.deepcopy((self.profile, self.job))

        result = self._build(style="warm", length="concise")

        self.assertEqual(result, self._build(style="warm", length="concise"))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "READY")
        self.assertIn("Application for Platform Engineer at Example Systems", result["preview_text"])
        self.assertIn("Build Python reporting services", result["preview_text"])
        self.assertNotIn("Kubernetes", result["preview_text"])
        self.assertEqual(result["profile_id"], "profile-fixture")
        self.assertEqual(result["profile_version"], self.profile_version)
        self.assertEqual(result["job_id"], self.job["id"])
        self.assertEqual(result["job_fingerprint"], self.job_fingerprint)
        self.assertEqual(set(result["fact_ids"]), {"python", "duty"})
        self.assertEqual(result["source_fact_ids"], result["fact_ids"])
        self.assertEqual(set(result["source_fact_fingerprints"]), {"python", "duty"})
        self.assertEqual(result["source_fact_fingerprints"]["python"],
                         source_fact_fingerprint(self.profile["facts"][0]))
        self.assertTrue(all(row["verified"] and row["user_approved"]
                            and len(row["sha256"]) == 64 for row in result["source_facts"]))
        self.assertNotIn("pending", str(result))
        self.assertEqual(result["skills"], ["Python", "SQL"])
        self.assertEqual(result["skill_strengths"], {"Python": "REQUIRED", "SQL": "PREFERRED"})
        self.assertTrue(result["responsibilities"])
        self.assertEqual(result["job_evidence"]["responsibility_source"], "SAVED_DESCRIPTION_PARSED")
        self.assertEqual(result["company_identity"], "VERIFIED_SOURCE")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["document_created"])
        self.assertIsNone(result["company_context"])
        self.assertIn("USER_PROVIDED / CONTENT UNVERIFIED", result["company_context_caveat"])
        self.assertEqual((self.profile, self.job), before)

    def test_stale_profile_or_job_snapshot_is_rejected(self):
        with self.assertRaises(PreparationError) as stale_profile:
            self._build(expected_profile_version="b" * 64)
        self.assertEqual(stale_profile.exception.code, "COVER_LETTER_PREVIEW_PROFILE_STALE")

        with self.assertRaises(PreparationError) as stale_job:
            self._build(expected_job_fingerprint="c" * 64)
        self.assertEqual(stale_job.exception.code, "COVER_LETTER_PREVIEW_JOB_STALE")

    def test_fact_provenance_requires_the_current_profile_identity(self):
        foreign_profile = copy.deepcopy(self.profile)
        foreign_profile["id"] = "another-profile-fixture"

        with self.assertRaises(PreparationError) as missing_identity:
            self._build(facts_profile_id=None)
        self.assertEqual(missing_identity.exception.code, "COVER_LETTER_PREVIEW_EVIDENCE_INVALID")

        with self.assertRaises(PreparationError) as foreign_identity:
            self._build(facts=foreign_profile["facts"], facts_profile_id=foreign_profile["id"])
        self.assertEqual(foreign_identity.exception.code, "COVER_LETTER_PREVIEW_EVIDENCE_INVALID")

    def test_full_preview_fingerprint_detects_company_and_context_only_changes(self):
        changed_company = copy.deepcopy(self.job)
        changed_company["company"] = "Different Employer"
        self.assertNotEqual(cover_letter_preview_fingerprint(changed_company), self.job_fingerprint)
        with self.assertRaises(PreparationError) as stale_company:
            self._build(job=changed_company)
        self.assertEqual(stale_company.exception.code, "COVER_LETTER_PREVIEW_JOB_STALE")

        changed_context = copy.deepcopy(self.job)
        changed_context["attributes"]["approved_company_context"] = {"version": 1}
        self.assertNotEqual(cover_letter_preview_fingerprint(changed_context), self.job_fingerprint)

    def test_missing_approved_matching_facts_fail_closed(self):
        profile = copy.deepcopy(self.profile)
        profile["facts"] = [profile["facts"][-1]]

        with self.assertRaises(PreparationError) as error:
            self._build(profile=profile, facts=profile["facts"])

        self.assertEqual(error.exception.code, "COVER_LETTER_RELEVANT_FACTS_MISSING")

    def test_missing_responsibility_context_fails_closed(self):
        job = copy.deepcopy(self.job)
        job["description"] = "Python and SQL appear in this posting."

        with self.assertRaises(PreparationError) as error:
            self._build(job=job, expected_job_fingerprint=cover_letter_preview_fingerprint(job))

        self.assertEqual(error.exception.code, "COVER_LETTER_JOB_CONTEXT_MISSING")

    def test_source_facts_must_match_current_profile_and_have_unique_identity(self):
        tampered = copy.deepcopy(self.profile["facts"])
        tampered[0]["text"] = "Python and fabricated claims"
        with self.assertRaises(PreparationError) as changed:
            self._build(facts=tampered)
        self.assertEqual(changed.exception.code, "COVER_LETTER_PREVIEW_EVIDENCE_INVALID")

        duplicate = [*self.profile["facts"], copy.deepcopy(self.profile["facts"][0])]
        with self.assertRaises(PreparationError) as ambiguous:
            self._build(facts=duplicate)
        self.assertEqual(ambiguous.exception.code, "COVER_LETTER_PREVIEW_EVIDENCE_INVALID")

    def test_user_provided_company_context_remains_caveated_and_unverified(self):
        content = "Example Systems maintains cloud reporting services."
        context = {
            "id": "context-fixture", "company_id": self.job["company_id"],
            "company_source_id": "source-fixture", "version": 1,
            "content_origin": "USER_PROVIDED", "content_status": "UNVERIFIED",
            "content": content, "source_url": "https://example.test/about",
            "source_host": "example.test", "verified_domain": "example.test",
            "source_identity_method": "VERIFIED_EXISTING_DOMAIN", "identity_status": "VERIFIED",
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "user_reviewed": True, "user_approved": True, "reviewed_at": "2026-10-09T00:00:00Z",
        }
        job = copy.deepcopy(self.job)
        job["attributes"]["approved_company_context"] = context

        result = self._build(job=job, expected_job_fingerprint=cover_letter_preview_fingerprint(job))

        self.assertIsNotNone(result["company_context"])
        self.assertIn("USER_PROVIDED / CONTENT UNVERIFIED", result["company_context"]["caveat"])
        self.assertEqual(result["company_context"]["content_origin"], "USER_PROVIDED")
        self.assertEqual(result["company_context"]["content_status"], "UNVERIFIED")
        self.assertTrue(result["company_context_evidence"]["included"])
        self.assertIn("USER_PROVIDED; CONTENT UNVERIFIED", result["preview_text"])
        self.assertIn(content, result["preview_text"])


if __name__ == "__main__":
    unittest.main()
