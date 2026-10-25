"""Sustained regression coverage for generated-document fact traces (MJ-REQ-269)."""
import copy
import json
import re
import unittest

from services.documents.cover_letter_preview import (
    build_cover_letter_preview,
    cover_letter_preview_fingerprint,
)
from services.documents.errors import PreparationError
from services.documents.tailor import source_fact_fingerprint


class DocumentFactTraceSustainTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "id": "fact-trace-profile-fixture",
            "version": "a" * 64,
            "name": "Synthetic Applicant",
            "facts": [
                {
                    "fact_id": "python",
                    "kind": "skill",
                    "text": "PRIVATE_PYTHON_FACT_MARKER Python service development for internal reporting.",
                    "source": "approved resume section: skills",
                    "confidence": 98,
                    "verified": True,
                    "user_approved": True,
                },
                {
                    "fact_id": "duty",
                    "kind": "responsibility",
                    "text": "PRIVATE_DUTY_FACT_MARKER Built Python services for internal reporting teams.",
                    "source": "approved resume section: experience",
                    "confidence": 96,
                    "verified": True,
                    "user_approved": True,
                },
                {
                    "fact_id": "unapproved",
                    "kind": "skill",
                    "text": "PRIVATE_UNAPPROVED_MARKER Kubernetes administration.",
                    "source": "unreviewed import",
                    "confidence": 100,
                    "verified": True,
                    "user_approved": False,
                },
            ],
        }
        self.job = {
            "id": "j" * 64,
            "company_id": "synthetic-company",
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
            **options,
        )

    @staticmethod
    def _trace(result):
        return {
            "fact_ids": result["fact_ids"],
            "source_fact_ids": result["source_fact_ids"],
            "source_fact_fingerprints": result["source_fact_fingerprints"],
            "source_facts": result["source_facts"],
            "matched_terms": result["matched_terms"],
        }

    def test_selected_fact_trace_is_deterministic_bounded_and_serializable_without_raw_text(self):
        result = self._build()
        repeated = self._build()
        trace = self._trace(result)

        self.assertEqual(trace, self._trace(repeated))
        self.assertEqual(result["status"], "READY")
        self.assertFalse(result["document_created"])
        self.assertEqual(result["fact_ids"], ["duty", "python"])
        self.assertEqual(result["source_fact_ids"], result["fact_ids"])
        self.assertEqual(
            trace["matched_terms"],
            {
                "duty": ["internal", "python", "reporting", "services", "teams"],
                "python": ["internal", "python", "reporting"],
            },
        )
        self.assertTrue(all(isinstance(fact_id, str) and 0 < len(fact_id) <= 128
                            for fact_id in trace["source_fact_ids"]))
        self.assertEqual(set(trace["source_fact_fingerprints"]), set(trace["source_fact_ids"]))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                            for fingerprint in trace["source_fact_fingerprints"].values()))
        expected_fingerprints = {
            fact["fact_id"]: source_fact_fingerprint(fact)
            for fact in self.profile["facts"]
            if fact["fact_id"] in trace["source_fact_ids"]
        }
        self.assertEqual(trace["source_fact_fingerprints"], expected_fingerprints)
        self.assertEqual(
            {row["fact_id"]: row["matched_terms"] for row in trace["source_facts"]},
            trace["matched_terms"],
        )
        self.assertTrue(all("text" not in row for row in trace["source_facts"]))
        trace_json = json.dumps(trace, sort_keys=True, separators=(",", ":"))
        self.assertNotIn("PRIVATE_PYTHON_FACT_MARKER", trace_json)
        self.assertNotIn("PRIVATE_DUTY_FACT_MARKER", trace_json)
        self.assertNotIn("PRIVATE_UNAPPROVED_MARKER", trace_json)
        self.assertEqual(json.loads(trace_json), trace)

    def test_missing_caller_provenance_fails_closed(self):
        with self.assertRaises(PreparationError) as missing:
            self._build(facts=[])

        self.assertEqual(missing.exception.code, "COVER_LETTER_PREVIEW_EVIDENCE_INVALID")

    def test_fact_without_source_provenance_cannot_be_traced_or_previewed(self):
        profile = copy.deepcopy(self.profile)
        for fact in profile["facts"]:
            if fact["user_approved"]:
                fact.pop("source")

        with self.assertRaises(PreparationError) as missing_source:
            self._build(profile=profile)

        self.assertEqual(missing_source.exception.code, "COVER_LETTER_RELEVANT_FACTS_MISSING")

    def test_tampered_approval_provenance_fails_closed_before_trace(self):
        supplied = copy.deepcopy(self.profile["facts"])
        supplied[0]["user_approved"] = False

        with self.assertRaises(PreparationError) as tampered:
            self._build(facts=supplied)

        self.assertEqual(tampered.exception.code, "COVER_LETTER_PREVIEW_EVIDENCE_INVALID")


if __name__ == "__main__":
    unittest.main()