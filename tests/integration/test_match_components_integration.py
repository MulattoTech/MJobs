"""Integration tests for Match Components (MJ-REQ-113)."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.matching.components import evaluate_match_components


PROFILE = {
    "id": "profile-components-int",
    "name": "Sam Architect",
    "email": "sam@architect.test",
    "preferences": {
        "desired_roles": ["Senior Platform Engineer"],
        "work_arrangements": ["REMOTE"],
        "employment_types": ["FULL_TIME"],
        "salary_expectations": "$140,000 annually",
    },
    "facts": [
        {"id": "fact-1", "kind": "title", "text": "Senior Platform Engineer", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "fact-2", "kind": "skill", "text": "Python", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "fact-3", "kind": "skill", "text": "PostgreSQL", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "fact-4", "kind": "experience", "text": "Architected distributed systems in Python", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
        {"id": "fact-5", "kind": "education", "text": "Master of Science in Software Engineering", "source": "RESUME", "user_approved": True, "verified": True, "confidence": 1.0},
    ],
}


class MatchComponentsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-match-components-int-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'match_components.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))

    def _seed_job(self, job_id: str, title: str, attributes: dict):
        company_id = f"comp-{job_id}"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name="Platform Corp", domain="platformcorp.example",
            ))
            connection.execute(m.jobs.insert().values(
                id=job_id,
                company_id=company_id,
                requisition=f"req-{job_id}",
                title=title,
                description="High scale platform role",
                skills=["Python", "PostgreSQL"],
                location="Remote",
                remote_type="REMOTE",
                attributes=attributes,
            ))

    def test_database_job_evaluation(self):
        attrs = {
            "required_skills": ["Python", "PostgreSQL"],
            "education_requirement": "BACHELORS_REQUIRED",
            "salary_min": 150000,
            "salary_max": 180000,
            "employment_type": "FULL_TIME",
        }
        self._seed_job("job-platform-1", "Senior Platform Engineer", attrs)

        with self.store.engine.connect() as connection:
            job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == "job-platform-1")).mappings().one())

        profile = self.store.profile(PROFILE["id"])
        result = evaluate_match_components(job, profile)

        self.assertIsNotNone(result["score"])
        self.assertGreaterEqual(result["score"], 90.0)
        self.assertTrue(result["advisory"])
        self.assertEqual(len(result["dimensions"]), 12)

        # Check that known dimensions reflect actual facts
        self.assertIn("title_similarity", result["known_dimensions"])
        self.assertIn("required_skills", result["known_dimensions"])
        self.assertIn("education", result["known_dimensions"])
        self.assertIn("salary", result["known_dimensions"])

    def test_negative_requirements_dealbreaker(self):
        # Seed a job with avoid terms and dealbreaker clearance
        attrs = {
            "required_skills": ["Python"],
            "security_clearance_required": True,
            "polygraph_required": True,
        }
        self._seed_job("job-clearance-1", "Security Engineer", attrs)

        with self.store.engine.connect() as connection:
            job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == "job-clearance-1")).mappings().one())

        profile = deepcopy(PROFILE)
        profile["id"] = "profile-avoid"
        profile["preferences"]["avoid_terms"] = ["polygraph", "crypto"]
        self.store.save_profile(profile)

        saved_profile = self.store.profile("profile-avoid")
        result = evaluate_match_components(job, saved_profile)

        self.assertIn("polygraph", result["negative_signals_triggered"])
        # Negative requirements trigger penalty should reduce raw score significantly
        neg_dim = next(d for d in result["dimensions"] if d["dimension"] == "negative_requirements")
        self.assertEqual(neg_dim["raw_score"], 0.0)

    def test_sparse_job_unknown_dimensions(self):
        # Seed job with zero metadata/attributes
        self._seed_job("job-sparse-1", "Engineer", {})

        with self.store.engine.connect() as connection:
            job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == "job-sparse-1")).mappings().one())

        profile = self.store.profile(PROFILE["id"])
        result = evaluate_match_components(job, profile)

        self.assertIn("certifications", result["unknown_dimensions"])
        self.assertIn("industry", result["unknown_dimensions"])
        self.assertIn("company_preference", result["unknown_dimensions"])
        self.assertLess(result["coverage_percent"], 100.0)
