"""Unit tests for Requirement Strength & Differential Scoring (MJ-REQ-115)."""
import unittest

from services.matching.requirement_strength import (
    STRENGTH_WEIGHTS,
    analyze_qualification_strengths,
    classify_strength_from_clause,
    extract_qualification_items,
)


class QualificationStrengthUnitTests(unittest.TestCase):
    def test_classify_strength_from_clause(self):
        # Mandatory patterns
        self.assertEqual(classify_strength_from_clause("Candidate must have Python experience"), "REQUIRED")
        self.assertEqual(classify_strength_from_clause("5+ years Linux required"), "REQUIRED")
        self.assertEqual(classify_strength_from_clause("Essential: PostgreSQL database tuning"), "REQUIRED")
        self.assertEqual(classify_strength_from_clause("Mandatory qualification: Bachelor's in CS"), "REQUIRED")

        # Preferred patterns
        self.assertEqual(classify_strength_from_clause("Kubernetes experience preferred"), "PREFERRED")
        self.assertEqual(classify_strength_from_clause("Strongly preferred: AWS Solutions Architect certification"), "PREFERRED")
        self.assertEqual(classify_strength_from_clause("Prior experience in fintech desired"), "PREFERRED")

        # Nice to have patterns
        self.assertEqual(classify_strength_from_clause("Nice to have: experience with GraphQL"), "NICE_TO_HAVE")
        self.assertEqual(classify_strength_from_clause("Bonus if familiar with Rust or Go"), "NICE_TO_HAVE")
        self.assertEqual(classify_strength_from_clause("Familiarity with Redis is a plus"), "NICE_TO_HAVE")

        # Unknown / unclassified
        self.assertEqual(classify_strength_from_clause("We work on modern distributed systems"), "UNKNOWN")

    def test_mathematical_penalty_differential(self):
        # MJ-REQ-115: Missing a preferred technology must not penalize as severely as missing mandatory qualification
        req_weight = STRENGTH_WEIGHTS["REQUIRED"]
        pref_weight = STRENGTH_WEIGHTS["PREFERRED"]
        nice_weight = STRENGTH_WEIGHTS["NICE_TO_HAVE"]

        self.assertLess(pref_weight, req_weight)
        self.assertEqual(pref_weight, 0.5 * req_weight)
        self.assertLess(nice_weight, pref_weight)

    def test_extract_and_analyze_qualification_strengths(self):
        job = {
            "id": "job-strength-1",
            "title": "Principal Infrastructure Engineer",
            "description": "Must have Python. Kubernetes preferred. Bonus: Terraform.",
            "skills": ["Linux"],
            "attributes": {
                "required_skills": ["Python", "PostgreSQL"],
                "preferred_skills": ["Kubernetes", "Docker"],
                "nice_to_have_skills": ["Terraform"],
            },
        }

        # Candidate has Python and Docker verified, but lacks PostgreSQL and Kubernetes
        profile = {
            "id": "cand-1",
            "facts": [
                {"kind": "skill", "text": "Python", "user_approved": True, "verified": True},
                {"kind": "skill", "text": "Docker", "user_approved": True, "verified": True},
            ],
        }

        analysis = analyze_qualification_strengths(job, profile)

        self.assertTrue(analysis["penalty_differential_valid"])
        counts = analysis["counts"]

        # Required: Python (covered), PostgreSQL (missing) -> 1/2 = 50%
        self.assertEqual(counts["REQUIRED"]["total"], 2)
        self.assertEqual(counts["REQUIRED"]["covered"], 1)
        self.assertEqual(counts["REQUIRED"]["missing"], 1)
        self.assertEqual(analysis["required_coverage_percent"], 50.0)

        # Preferred: Kubernetes (missing), Docker (covered) -> 1/2 = 50%
        self.assertEqual(counts["PREFERRED"]["total"], 2)
        self.assertEqual(counts["PREFERRED"]["covered"], 1)
        self.assertEqual(counts["PREFERRED"]["missing"], 1)
        self.assertEqual(analysis["preferred_coverage_percent"], 50.0)

        # Nice to have: Terraform (missing) -> 0/1 = 0%
        self.assertEqual(counts["NICE_TO_HAVE"]["total"], 1)
        self.assertEqual(counts["NICE_TO_HAVE"]["missing"], 1)

        # Missing required vs missing preferred item impact verification
        missing_req_item = next(i for i in analysis["by_strength"]["REQUIRED"] if i["candidate_status"] == "MISSING")
        missing_pref_item = next(i for i in analysis["by_strength"]["PREFERRED"] if i["candidate_status"] == "MISSING")

        self.assertEqual(missing_req_item["weight"], 1.0)
        self.assertEqual(missing_pref_item["weight"], 0.5)
        self.assertIn("mandatory", missing_req_item["score_impact"].casefold())
        self.assertIn("desirable but not disqualifying", missing_pref_item["score_impact"].casefold())


if __name__ == "__main__":
    unittest.main()
