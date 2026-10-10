"""Integration tests for Requirement Strength & Differential Scoring (MJ-REQ-115)."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.matching.requirement_strength import analyze_qualification_strengths


PROFILE = {
    "id": "profile-strength-int",
    "name": "Jordan Engineer",
    "email": "jordan@engineer.test",
    "preferences": {
        "desired_roles": ["Lead Cloud Platform Engineer"],
        "work_arrangements": ["REMOTE"],
        "employment_types": ["FULL_TIME"],
        "salary_expectations": "$150,000 annually",
    },
    "facts": [
        {"id": "f-1", "kind": "title", "text": "Lead Cloud Platform Engineer", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "f-2", "kind": "skill", "text": "Python", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "f-3", "kind": "skill", "text": "PostgreSQL", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "f-4", "kind": "certification", "text": "AWS Solutions Architect Associate", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "f-5", "kind": "education", "text": "Bachelor of Science in Computer Science", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
    ],
}


class QualificationStrengthIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-strength-int-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'strength.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))

    def _seed_job(self, job_id: str, title: str, description: str, skills: list[str], attributes: dict):
        company_id = f"comp-{job_id}"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name="Fintech Services Corp", domain="fintechcorp.example",
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

    def test_database_job_qualification_strength_analysis(self):
        desc = (
            "We are seeking a Lead Cloud Platform Engineer. "
            "Python and PostgreSQL are mandatory requirements. "
            "Kubernetes and Terraform are preferred qualifications. "
            "Golang experience is a plus."
        )
        attrs = {
            "required_skills": ["Python", "PostgreSQL"],
            "preferred_skills": ["Kubernetes", "Terraform"],
            "nice_to_have_skills": ["Golang"],
        }
        self._seed_job("job-fintech-1", "Lead Cloud Platform Engineer", desc, ["Python", "PostgreSQL", "Kubernetes"], attrs)

        with self.store.engine.connect() as connection:
            db_job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == "job-fintech-1")).mappings().one())

        profile = self.store.profile(PROFILE["id"])
        analysis = analyze_qualification_strengths(db_job, profile)

        # 1. 100% of required skills are met (Python, PostgreSQL)
        self.assertEqual(analysis["required_coverage_percent"], 100.0)
        self.assertEqual(analysis["counts"]["REQUIRED"]["covered"], 2)
        self.assertEqual(analysis["counts"]["REQUIRED"]["missing"], 0)

        # 2. 0% of preferred skills are met (Kubernetes, Terraform)
        self.assertEqual(analysis["preferred_coverage_percent"], 0.0)
        self.assertEqual(analysis["counts"]["PREFERRED"]["covered"], 0)
        self.assertEqual(analysis["counts"]["PREFERRED"]["missing"], 2)

        # 3. Penalty differential bound: missing 2 preferred skills (0.5 + 0.5 = 1.0 weight)
        # does not penalize as severely as missing the 2 required skills (1.0 + 1.0 = 2.0 weight)
        self.assertTrue(analysis["penalty_differential_valid"])

        # 4. Overall score reflects weighted alignment:
        # Total weight: 2 * 1.0 (req) + 2 * 0.5 (pref) + 1 * 0.25 (nice) = 3.25
        # Covered: 2 * 1.0 = 2.0 -> 2.0 / 3.25 = 61.5%
        self.assertGreaterEqual(analysis["overall_strength_score"], 60.0)


if __name__ == "__main__":
    unittest.main()
