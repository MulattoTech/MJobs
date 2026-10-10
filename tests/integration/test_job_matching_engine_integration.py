"""Integration tests for Job Matching Engine (MJ-REQ-112)."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.matching.engine import (
    batch_score_job_matches,
    classify_match_category,
    format_match_score,
    score_job_match,
)


CANDIDATE_PROFILE = {
    "id": "profile-match-engine-int",
    "name": "Alex Architect",
    "email": "alex@architect.test",
    "preferences": {
        "desired_roles": ["Staff Platform Architect", "Principal Platform Engineer"],
        "work_arrangements": ["REMOTE"],
        "employment_types": ["FULL_TIME"],
        "salary_expectations": "$160,000 annually",
        "avoid_terms": ["crypto", "gambling"],
    },
    "facts": [
        {"id": "fact-1", "kind": "title", "text": "Staff Platform Architect", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "fact-2", "kind": "skill", "text": "Python", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "fact-3", "kind": "skill", "text": "PostgreSQL", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "fact-4", "kind": "skill", "text": "Kubernetes", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "fact-5", "kind": "experience", "text": "12 years designing fault-tolerant platform architectures in Python", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "fact-6", "kind": "education", "text": "Master of Science in Distributed Systems", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
    ],
}


class JobMatchingEngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-match-engine-int-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'match_engine.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(CANDIDATE_PROFILE))

    def _seed_job(self, job_id: str, title: str, description: str, skills: list[str], attributes: dict):
        company_id = f"comp-{job_id}"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name="Cloud Services Inc", domain="cloud.example",
            ))
            connection.execute(m.jobs.insert().values(
                id=job_id,
                company_id=company_id,
                requisition=f"req-{job_id}",
                title=title,
                description=description,
                skills=skills,
                location="Remote - US",
                remote_type="REMOTE",
                attributes=attributes,
            ))

    def test_database_jobs_batch_scoring_and_formatting(self):
        # 1. High match job
        self._seed_job(
            "job-high-1",
            "Staff Platform Architect",
            "High scale platform architecture utilizing Python, PostgreSQL, and Kubernetes.",
            ["Python", "PostgreSQL", "Kubernetes"],
            {
                "required_skills": ["Python", "PostgreSQL"],
                "education_requirement": "MASTERS_REQUIRED",
                "salary_min": 170000,
                "salary_max": 210000,
                "employment_type": "FULL_TIME",
            },
        )

        # 2. Dealbreaker avoid term job
        self._seed_job(
            "job-dealbreaker-1",
            "Crypto Platform Architect",
            "High frequency crypto trading platform architecture in Python.",
            ["Python", "Crypto"],
            {
                "required_skills": ["Python"],
                "salary_min": 180000,
                "employment_type": "FULL_TIME",
            },
        )

        # 3. Moderate fit job
        self._seed_job(
            "job-mod-1",
            "General Software Developer",
            "C++ firmware and embedded hardware driver development.",
            ["C++", "Embedded"],
            {
                "required_skills": ["C++"],
                "employment_type": "FULL_TIME",
            },
        )

        with self.store.engine.connect() as connection:
            db_jobs = list(connection.execute(select(m.jobs).order_by(m.jobs.c.id)).mappings())

        profile = self.store.profile(CANDIDATE_PROFILE["id"])
        results = batch_score_job_matches(db_jobs, profile, sort_descending=True)

        self.assertEqual(len(results), 3)

        # First sorted job should be the high alignment job
        top_result = results[0]
        self.assertEqual(top_result["job_id"], "job-high-1")
        self.assertIsNotNone(top_result["score"])
        self.assertGreaterEqual(top_result["score"], 85.0)
        self.assertIn(top_result["category"], ("EXCEPTIONAL", "EXCELLENT"))
        self.assertIn("/100 — ", top_result["score_formatted"])
        self.assertTrue(top_result["advisory"])
        self.assertIn("High alignment", top_result["recommendation"])

        # Second or third should include the dealbreaker job with triggered signals
        dealbreaker_res = next(r for r in results if r["job_id"] == "job-dealbreaker-1")
        self.assertIn("crypto", dealbreaker_res["negative_signals_triggered"])
        self.assertIn("Caution: Triggers dealbreaker avoidance term(s)", dealbreaker_res["recommendation"])

    def test_canonical_score_formatting_examples(self):
        # Verify formatting complies with 91/100 — Excellent Match requirement
        self.assertEqual(format_match_score(91.0), "91/100 — Excellent Match")
        self.assertEqual(format_match_score(97.2), "97.2/100 — Exceptional Match")
        self.assertEqual(format_match_score(78.0), "78/100 — Strong Match")
        self.assertEqual(format_match_score(64.0), "64/100 — Possible Match")
        self.assertEqual(format_match_score(48.0), "48/100 — Weak Match")
        self.assertEqual(format_match_score(25.0), "25/100 — Poor Match")


if __name__ == "__main__":
    unittest.main()
