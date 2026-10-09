"""User-review-only previews for factual resume rewrites."""
import copy
import hashlib
import unittest

from services.documents.errors import PreparationError
from services.documents.rewrite_review import build_rewrite_review
from services.documents.tailor import target_fingerprint


class ResumeRewriteReviewTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "id": "profile-a",
            "version": "a" * 64,
            "facts": [
                {
                    "fact_id": "credential-work",
                    "kind": "experience",
                    "text": "Maintained Cisco CCNA systems for 12 years since January 2021.",
                    "source": "fictional-test-profile",
                    "confidence": 95,
                    "verified": True,
                    "user_approved": True,
                },
                {
                    "fact_id": "unapproved",
                    "kind": "experience",
                    "text": "Pending experience fact",
                    "source": "fictional-test-profile",
                    "confidence": 95,
                    "verified": True,
                    "user_approved": False,
                },
                {
                    "fact_id": "wrong-use",
                    "kind": "experience",
                    "text": "Python",
                    "source": "fictional-test-profile",
                    "confidence": 100,
                    "verified": True,
                    "user_approved": True,
                    "allowed_uses": ["APPLICATION_ANSWER"],
                },
                {
                    "fact_id": "unsupported-kind",
                    "kind": "skill",
                    "text": "Kubernetes",
                    "source": "fictional-test-profile",
                    "confidence": 100,
                    "verified": True,
                    "user_approved": True,
                    "allowed_uses": ["RESUME"],
                },
            ],
        }
        self.job = {
            "id": "j" * 64,
            "title": "Systems Administrator",
            "skills": ["Linux", "Cisco"],
            "description": "Responsibilities:\n- Maintain systems and restore production services.",
        }
        self.expected_profile_version = self.profile["version"]
        self.expected_job_fingerprint = target_fingerprint(self.job)

    def _build(self, ids=None, rewrites=None, **overrides):
        return build_rewrite_review(
            overrides.get("profile", self.profile),
            overrides.get("job", self.job),
            ids if ids is not None else ["credential-work"],
            rewrites if rewrites is not None else [{
                "fact_id": "credential-work",
                "text": "Supported Cisco CCNA services for 12 years since January 2021.",
            }],
            overrides.get("expected_profile_version", self.expected_profile_version),
            overrides.get("expected_job_fingerprint", self.expected_job_fingerprint),
        )

    def test_review_preview_pairs_exact_source_and_proposal_with_provenance(self):
        original_profile = copy.deepcopy(self.profile)
        original_job = copy.deepcopy(self.job)
        result = self._build()

        self.assertEqual(result["profile_id"], "profile-a")
        self.assertEqual(result["profile_version"], self.profile["version"])
        self.assertEqual(result["job_id"], self.job["id"])
        self.assertEqual(result["job_fingerprint"], self.expected_job_fingerprint)
        self.assertEqual(result["selected_fact_ids"], ["credential-work"])
        self.assertEqual(len(result["rewrites"]), 1)
        row = result["rewrites"][0]
        self.assertEqual(row["fact_id"], "credential-work")
        self.assertEqual(row["fact_kind"], "experience")
        self.assertEqual(row["source_text"], self.profile["facts"][0]["text"])
        self.assertEqual(row["source_sha256"], hashlib.sha256(
            self.profile["facts"][0]["text"].encode("utf-8")).hexdigest())
        self.assertEqual(row["proposed_text"], "Supported Cisco CCNA services for 12 years since January 2021.")
        audit = row["protected_token_audit"]
        self.assertTrue(audit["required"])
        self.assertEqual(audit["missing"], [])
        self.assertEqual(audit["retained"], audit["required"])
        self.assertEqual({token["type"] for token in audit["required"]},
                         {"DATE", "NUMBER", "NAMED_CREDENTIAL"})
        self.assertTrue(result["read_only"])
        self.assertTrue(result["review_required"])
        self.assertEqual(result["semantic_equivalence"], "USER_REVIEW_REQUIRED")
        self.assertNotIn("semantic_equivalence_proven", result)
        self.assertEqual(self.profile, original_profile)
        self.assertEqual(self.job, original_job)

    def test_missing_protected_tokens_are_reported_for_user_correction(self):
        original_profile = copy.deepcopy(self.profile)
        original_job = copy.deepcopy(self.job)
        result = self._build(rewrites=[{
            "fact_id": "credential-work",
            "text": "Supported enterprise services.",
        }])

        audit = result["rewrites"][0]["protected_token_audit"]
        self.assertTrue(audit["required"])
        self.assertEqual(audit["retained"], [])
        self.assertEqual(audit["missing"], audit["required"])
        self.assertTrue(result["review_required"])
        self.assertEqual(result["semantic_equivalence"], "USER_REVIEW_REQUIRED")
        self.assertEqual(self.profile, original_profile)
        self.assertEqual(self.job, original_job)

    def test_preview_is_deterministic_and_does_not_mutate_inputs(self):
        original_profile = copy.deepcopy(self.profile)
        original_job = copy.deepcopy(self.job)

        first = self._build()
        second = self._build()

        self.assertEqual(first, second)
        self.assertEqual(self.profile, original_profile)
        self.assertEqual(self.job, original_job)

    def test_stale_profile_or_job_snapshots_fail_closed(self):
        changed_profile = copy.deepcopy(self.profile)
        changed_profile["version"] = "b" * 64
        changed_profile["facts"][0]["text"] = "Updated source wording"
        changed_job = {**self.job, "skills": ["Windows"]}
        cases = [
            ({"profile": changed_profile}, "RESUME_REWRITE_PROFILE_STALE"),
            ({"job": changed_job}, "RESUME_REWRITE_JOB_STALE"),
        ]
        for kwargs, expected_code in cases:
            with self.subTest(keys=list(kwargs)), self.assertRaises(PreparationError) as raised:
                self._build(**kwargs)
            self.assertEqual(raised.exception.code, expected_code)

    def test_unapproved_missing_and_wrong_use_facts_fail_closed(self):
        for fact_id in ("unapproved", "missing", "wrong-use", "unsupported-kind"):
            with self.subTest(fact_id=fact_id), self.assertRaises(PreparationError) as raised:
                self._build(ids=[fact_id], rewrites=[{"fact_id": fact_id, "text": "A proposed rewrite."}])
            self.assertEqual(raised.exception.code, "RESUME_REWRITE_FACT_INVALID")

    def test_fact_ids_are_resolved_only_within_the_supplied_profile(self):
        other_profile = {
            **copy.deepcopy(self.profile),
            "id": "profile-b",
            "facts": [{
                "fact_id": "unrelated-fact", "kind": "experience", "text": "Unrelated",
                "source": "fictional-test-profile", "confidence": 95,
                "verified": True, "user_approved": True,
            }],
        }

        with self.assertRaises(PreparationError) as raised:
            self._build(profile=other_profile)

        self.assertEqual(raised.exception.code, "RESUME_REWRITE_FACT_INVALID")

    def test_client_cannot_tamper_with_source_text_or_hash(self):
        with self.assertRaises(PreparationError) as raised:
            self._build(rewrites=[{
                "fact_id": "credential-work",
                "text": "Supported Cisco CCNA services for 12 years since January 2021.",
                "source_text": "Forged current profile source",
                "source_sha256": "0" * 64,
            }])

        self.assertEqual(raised.exception.code, "RESUME_REWRITE_INVALID")

    def test_selected_ids_and_rewrite_ids_must_match_uniquely(self):
        with self.assertRaises(PreparationError) as raised:
            self._build(ids=["credential-work", "credential-work"])
        self.assertEqual(raised.exception.code, "RESUME_REWRITE_SELECTION_INVALID")

        with self.assertRaises(PreparationError) as raised:
            self._build(ids=["credential-work"], rewrites=[])
        self.assertEqual(raised.exception.code, "RESUME_REWRITE_SELECTION_INVALID")

    def test_invalid_rewrite_text_is_rejected(self):
        for text in ("", "Maintained Cisco CCNA systems for 12 years since January 2021.", "\x00bad"):
            with self.subTest(text=text), self.assertRaises(PreparationError):
                self._build(rewrites=[{"fact_id": "credential-work", "text": text}])


if __name__ == "__main__":
    unittest.main()
