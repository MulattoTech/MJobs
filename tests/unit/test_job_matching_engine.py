"""Unit tests for Job Matching Engine (MJ-REQ-112)."""
import unittest

from services.matching.engine import (
    batch_score_job_matches,
    classify_match_category,
    format_match_score,
    score_job_match,
)


class JobMatchingEngineUnitTests(unittest.TestCase):
    def test_classify_match_category(self):
        self.assertEqual(classify_match_category(98.0), ("EXCEPTIONAL", "Exceptional Match"))
        self.assertEqual(classify_match_category(95.0), ("EXCEPTIONAL", "Exceptional Match"))
        self.assertEqual(classify_match_category(91.0), ("EXCELLENT", "Excellent Match"))
        self.assertEqual(classify_match_category(85.0), ("EXCELLENT", "Excellent Match"))
        self.assertEqual(classify_match_category(80.0), ("STRONG", "Strong Match"))
        self.assertEqual(classify_match_category(75.0), ("STRONG", "Strong Match"))
        self.assertEqual(classify_match_category(68.5), ("POSSIBLE", "Possible Match"))
        self.assertEqual(classify_match_category(60.0), ("POSSIBLE", "Possible Match"))
        self.assertEqual(classify_match_category(45.0), ("WEAK", "Weak Match"))
        self.assertEqual(classify_match_category(40.0), ("WEAK", "Weak Match"))
        self.assertEqual(classify_match_category(20.0), ("POOR", "Poor Match"))
        self.assertEqual(classify_match_category(0.0), ("POOR", "Poor Match"))
        self.assertEqual(classify_match_category(None), ("INCOMPLETE", "Incomplete Match"))

    def test_format_match_score_canonical_example(self):
        # Authoritative spec example: 91/100 — Excellent Match
        self.assertEqual(format_match_score(91.0), "91/100 — Excellent Match")
        self.assertEqual(format_match_score(91), "91/100 — Excellent Match")
        self.assertEqual(format_match_score(98.0), "98/100 — Exceptional Match")
        self.assertEqual(format_match_score(77.5), "77.5/100 — Strong Match")
        self.assertEqual(format_match_score(None), "Incomplete — Insufficient Data")

    def test_score_job_match_complete_result(self):
        job = {
            "id": "job-eng-1",
            "title": "Senior Systems Engineer",
            "description": "Designing high-reliability cloud platforms in Python with PostgreSQL.",
            "skills": ["Python", "PostgreSQL", "Linux"],
            "remote_type": "REMOTE",
            "attributes": {
                "required_skills": ["Python", "PostgreSQL"],
                "salary_min": 150000,
                "salary_max": 180000,
                "employment_type": "FULL_TIME",
                "education_requirement": "BACHELORS_REQUIRED",
            },
        }

        profile = {
            "id": "prof-1",
            "preferences": {
                "desired_roles": ["Senior Systems Engineer"],
                "work_arrangements": ["REMOTE"],
                "employment_types": ["FULL_TIME"],
                "salary_min": 140000,
            },
            "facts": [
                {"kind": "title", "text": "Senior Systems Engineer", "user_approved": True, "verified": True},
                {"kind": "skill", "text": "Python", "user_approved": True, "verified": True},
                {"kind": "skill", "text": "PostgreSQL", "user_approved": True, "verified": True},
                {"kind": "skill", "text": "Linux", "user_approved": True, "verified": True},
                {"kind": "education", "text": "Bachelor of Science in Computer Science", "user_approved": True, "verified": True},
                {"kind": "experience", "text": "10 years building distributed platforms in Python", "user_approved": True, "verified": True},
            ],
        }

        result = score_job_match(job, profile)

        self.assertEqual(result["job_id"], "job-eng-1")
        self.assertIsNotNone(result["score"])
        self.assertGreaterEqual(result["score"], 85.0)
        self.assertIn(result["category"], ("EXCEPTIONAL", "EXCELLENT"))
        self.assertIn("/100 — ", result["score_formatted"])
        self.assertTrue(result["advisory"])
        self.assertGreaterEqual(result["coverage_percent"], 50.0)
        self.assertIn("title_similarity", result["known_dimensions"])
        self.assertIn("required_skills", result["known_dimensions"])
        self.assertEqual(len(result["negative_signals_triggered"]), 0)
        self.assertIn("High alignment", result["recommendation"])

    def test_batch_score_job_matches_sorting(self):
        profile = {
            "id": "prof-batch",
            "preferences": {
                "desired_roles": ["Senior Staff Architect"],
                "work_arrangements": ["REMOTE"],
            },
            "facts": [
                {"kind": "title", "text": "Senior Staff Architect", "user_approved": True},
                {"kind": "skill", "text": "Python", "user_approved": True},
            ],
        }

        job_high = {
            "id": "job-high",
            "title": "Senior Staff Architect",
            "remote_type": "REMOTE",
            "skills": ["Python"],
        }
        job_med = {
            "id": "job-med",
            "title": "Staff Architect",
            "remote_type": "REMOTE",
            "skills": ["Java"],
        }
        job_low = {
            "id": "job-low",
            "title": "Frontend Intern",
            "remote_type": "ON_SITE",
            "skills": ["CSS"],
        }

        batch = batch_score_job_matches([job_low, job_high, job_med], profile)

        self.assertEqual(len(batch), 3)
        self.assertEqual(batch[0]["job_id"], "job-high")
        self.assertEqual(batch[1]["job_id"], "job-med")
        self.assertEqual(batch[2]["job_id"], "job-low")
        self.assertGreaterEqual(batch[0]["score"], batch[1]["score"])
        self.assertGreaterEqual(batch[1]["score"], batch[2]["score"])


if __name__ == "__main__":
    unittest.main()
