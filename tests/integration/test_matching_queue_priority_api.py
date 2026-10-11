"""Integration tests for Fresh Job Queue Priority & Application Priority (MJ-REQ-118, MJ-REQ-119)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.matching.queue_priority import (
    calculate_job_queue_priority,
    order_queue_by_fresh_priority,
    summarize_queue_distribution,
    to_operation_dispatch_params,
)
from services.matching.application_priority import (
    evaluate_job_priority_full,
    explain_priority_divergence,
)


PROFILE = {
    "id": "profile-priority-int",
    "name": "Alex Candidate",
    "email": "alex@candidate.test",
    "answers": [],
    "facts": [],
}
NOW = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone.utc)


class MatchingQueuePriorityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-queue-priority-int-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'queue_priority.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))

    def _seed_job_with_match(self, job_id: str, title: str, posted_at: datetime | None, match_score: float):
        profile = self.store.profile(PROFILE["id"])
        company_id = f"comp-{job_id}"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name="Test Company", domain="example.test",
            ))
            attributes = {}
            if posted_at:
                attributes["reported_posted_at"] = posted_at.isoformat()
            connection.execute(m.jobs.insert().values(
                id=job_id,
                company_id=company_id,
                requisition=f"req-{job_id}",
                title=title,
                description="Engineered opportunity",
                skills=["python", "docker"],
                location="Remote",
                remote_type="REMOTE",
                attributes=attributes,
            ))
            connection.execute(m.matches.insert().values(
                id=f"match-{job_id}",
                job_id=job_id,
                profile_id=PROFILE["id"],
                profile_version=profile["version"],
                score=match_score,
                missing=[],
                components={
                    "method": "weighted_skill_evidence_coverage",
                    "matched": ["python"],
                },
            ))

    def test_queue_ordering_prioritizes_fresh_excellent_over_stale_high_score(self):
        # Job A: Fresh 4h ago, 89% score
        self._seed_job_with_match("job-fresh-exc", "Staff Cloud Architect", NOW - timedelta(hours=4), 89.0)
        # Job B: Stale 40d ago, 96% score
        self._seed_job_with_match("job-stale-hi", "Lead DevOps Engineer", NOW - timedelta(days=40), 96.0)
        # Job C: Recent 48h ago, 86% score
        self._seed_job_with_match("job-recent-exc", "Senior Infrastructure Dev", NOW - timedelta(hours=48), 86.0)
        # Job D: Fresh 2h ago, 35% score
        self._seed_job_with_match("job-fresh-poor", "Junior Graphic Artist", NOW - timedelta(hours=2), 35.0)

        with self.store.engine.connect() as connection:
            db_jobs = list(connection.execute(select(m.jobs)).mappings())
            db_matches = {row["job_id"]: row["score"] for row in connection.execute(select(m.matches)).mappings()}

        ordered = order_queue_by_fresh_priority(db_jobs, db_matches, now=NOW)
        ordered_ids = [j["id"] for j in ordered]

        # Fresh excellent job MUST be processed first
        self.assertEqual(ordered_ids[0], "job-fresh-exc")
        self.assertEqual(ordered[0]["queue_priority"], "CRITICAL")
        self.assertEqual(ordered[0]["dispatch_priority"], "HIGH")
        self.assertTrue(ordered[0]["expedited"])

        # Recent excellent job comes second (Tier 1 High)
        self.assertEqual(ordered_ids[1], "job-recent-exc")
        self.assertEqual(ordered[1]["queue_priority"], "HIGH")

        # Stale job and poor job are deferred to back of queue
        self.assertIn("job-stale-hi", ordered_ids[2:])
        self.assertIn("job-fresh-poor", ordered_ids[2:])

    def test_full_priority_evaluation_with_divergence(self):
        # Seed a job with low complexity and direct employer
        self._seed_job_with_match("job-full-eval", "Principal SRE", NOW - timedelta(hours=10), 90.0)

        with self.store.engine.connect() as connection:
            job = dict(connection.execute(select(m.jobs).where(m.jobs.c.id == "job-full-eval")).mappings().one())
            match = dict(connection.execute(select(m.matches).where(m.matches.c.job_id == "job-full-eval")).mappings().one())

        signals = {
            "match_score": match["score"],
            "posted_at": (NOW - timedelta(hours=10)).isoformat(),
            "direct_employer_availability": "DIRECT_EMPLOYER",
            "application_complexity": "LOW",
        }

        evaluated = evaluate_job_priority_full(job, match, now=NOW, signals=signals)

        self.assertEqual(evaluated["job_id"], "job-full-eval")
        self.assertEqual(evaluated["match_score"], 90.0)
        self.assertIsNotNone(evaluated["priority_score"])
        self.assertEqual(evaluated["queue"]["queue_priority"], "CRITICAL")
        self.assertTrue(evaluated["advisory"])
        self.assertFalse(evaluated["affects_match_score"])

        # Queue dispatch params mapping
        dispatch_params = to_operation_dispatch_params(evaluated["queue"], {"job_id": job["id"]})
        self.assertEqual(dispatch_params["dispatch_priority"], "HIGH")
        self.assertEqual(dispatch_params["queue_tier"], 0)
        self.assertTrue(dispatch_params["fresh_job_priority"])

    def test_queue_summary_diagnostics(self):
        self._seed_job_with_match("job-1", "A", NOW - timedelta(hours=2), 92.0)
        self._seed_job_with_match("job-2", "B", NOW - timedelta(hours=50), 88.0)
        self._seed_job_with_match("job-3", "C", NOW - timedelta(days=60), 50.0)

        with self.store.engine.connect() as connection:
            db_jobs = list(connection.execute(select(m.jobs)).mappings())
            db_matches = {row["job_id"]: row["score"] for row in connection.execute(select(m.matches)).mappings()}

        ordered = order_queue_by_fresh_priority(db_jobs, db_matches, now=NOW)
        summary = summarize_queue_distribution(ordered)

        self.assertEqual(summary["total_queued"], 3)
        self.assertEqual(summary["expedited_count"], 2)  # Critical + High
        self.assertEqual(summary["deferred_count"], 1)  # Stale
