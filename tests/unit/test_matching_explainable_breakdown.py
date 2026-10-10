"""Unit and contract tests for Explainable Matching Breakdown (MJ-REQ-114)."""

import unittest

from services.matching.explainable_breakdown import (
    MatchFactorItem,
    build_explainable_match_breakdown,
    format_canonical_breakdown_text,
    generate_job_match_explanation,
    parse_explainable_text_block,
)


CANONICAL_SPEC_EXAMPLE = """+20 System Administrator title match
+12 Active Directory
+10 Windows Server
+8 PowerShell
+7 Microsoft 365
+6 VMware
+6 Remote US
+5 salary range
+5 relevant infrastructure experience

-5 bachelor's preferred
-3 Azure certification preferred

Score: 91"""


class ExplainableBreakdownUnitTests(unittest.TestCase):
    def test_canonical_spec_example_parsing_and_formatting(self):
        breakdown = parse_explainable_text_block(CANONICAL_SPEC_EXAMPLE)

        self.assertEqual(breakdown["total_score"], 91)
        self.assertEqual(breakdown["category"], "EXCELLENT")
        self.assertEqual(len(breakdown["positive_factors"]), 9)
        self.assertEqual(len(breakdown["negative_factors"]), 2)

        # Check positive factors
        self.assertEqual(breakdown["positive_factors"][0]["formatted_line"], "+20 System Administrator title match")
        self.assertEqual(breakdown["positive_factors"][1]["formatted_line"], "+12 Active Directory")
        self.assertEqual(breakdown["positive_factors"][2]["formatted_line"], "+10 Windows Server")
        self.assertEqual(breakdown["positive_factors"][3]["formatted_line"], "+8 PowerShell")
        self.assertEqual(breakdown["positive_factors"][4]["formatted_line"], "+7 Microsoft 365")
        self.assertEqual(breakdown["positive_factors"][5]["formatted_line"], "+6 VMware")
        self.assertEqual(breakdown["positive_factors"][6]["formatted_line"], "+6 Remote US")
        self.assertEqual(breakdown["positive_factors"][7]["formatted_line"], "+5 salary range")
        self.assertEqual(breakdown["positive_factors"][8]["formatted_line"], "+5 relevant infrastructure experience")

        # Check negative factors
        self.assertEqual(breakdown["negative_factors"][0]["formatted_line"], "-5 bachelor's preferred")
        self.assertEqual(breakdown["negative_factors"][1]["formatted_line"], "-3 Azure certification preferred")

        # Verify formatted text matches canonical spec verbatim
        self.assertEqual(breakdown["formatted_text"], CANONICAL_SPEC_EXAMPLE)

    def test_build_explainable_match_breakdown_math(self):
        positives = [
            MatchFactorItem(points=30, description="Senior Python Developer title"),
            MatchFactorItem(points=20, description="FastAPI expertise"),
            MatchFactorItem(points=20, description="PostgreSQL"),
            MatchFactorItem(points=10, description="Remote work"),
        ]
        negatives = [
            MatchFactorItem(points=-5, description="AWS certification preferred"),
        ]

        breakdown = build_explainable_match_breakdown(
            positive_factors=positives,
            negative_factors=negatives,
        )

        self.assertEqual(breakdown["positive_points_sum"], 80)
        self.assertEqual(breakdown["negative_points_deduction"], 5)
        self.assertEqual(breakdown["total_score"], 75)
        self.assertEqual(breakdown["category"], "STRONG")
        self.assertIn("+30 Senior Python Developer title", breakdown["formatted_text"])
        self.assertIn("-5 AWS certification preferred", breakdown["formatted_text"])
        self.assertIn("Score: 75", breakdown["formatted_text"])

    def test_clamping_to_zero_and_hundred(self):
        # Overflow capped at 100
        over_pos = [MatchFactorItem(points=120, description="Over-qualified match")]
        b1 = build_explainable_match_breakdown(positive_factors=over_pos)
        self.assertEqual(b1["total_score"], 100)
        self.assertEqual(b1["category"], "EXCEPTIONAL")

        # Underflow floored at 0
        huge_neg = [MatchFactorItem(points=-150, description="Missing all requirements")]
        b2 = build_explainable_match_breakdown(positive_factors=[], negative_factors=huge_neg)
        self.assertEqual(b2["total_score"], 0)
        self.assertEqual(b2["category"], "POOR")

    def test_generate_job_match_explanation_from_profile_and_job(self):
        job = {
            "title": "Cloud Infrastructure Engineer",
            "remote_type": "REMOTE_US",
            "skills": ["Kubernetes", "Terraform", "AWS"],
            "attributes": {
                "required_skills": ["Kubernetes", "Terraform"],
                "preferred_skills": ["AWS"],
                "experience_level": "SENIOR",
                "education_requirement": "BACHELORS_REQUIRED",
                "salary_min": 130000,
                "salary_max": 160000,
                "employment_type": "FULL_TIME",
                "industry": "Cloud Infrastructure",
            },
            "description": "Leading cloud infrastructure and automation.",
        }
        profile = {
            "preferences": {
                "desired_titles": ["Cloud Infrastructure Engineer", "DevOps Engineer"],
                "remote": "REMOTE",
                "employment_types": ["FULL_TIME"],
                "salary_min": 120000,
            },
            "facts": [
                {"kind": "title", "text": "Cloud Infrastructure Engineer", "user_approved": True, "verified": True},
                {"kind": "skill", "text": "Kubernetes", "user_approved": True, "verified": True},
                {"kind": "skill", "text": "Terraform", "user_approved": True, "verified": True},
                {"kind": "skill", "text": "AWS", "user_approved": True, "verified": True},
                {"kind": "experience", "text": "5 years managing cloud infrastructure", "user_approved": True, "verified": True},
                {"kind": "education", "text": "Bachelor of Science in Computer Science", "user_approved": True, "verified": True},
            ],
        }

        breakdown = generate_job_match_explanation(job, profile)

        self.assertGreaterEqual(breakdown["total_score"], 80)
        self.assertIn(breakdown["category"], ("EXCELLENT", "EXCEPTIONAL"))
        self.assertTrue(len(breakdown["positive_factors"]) > 0)
        self.assertTrue(any("Cloud" in f["description"] for f in breakdown["positive_factors"]))


if __name__ == "__main__":
    unittest.main()
