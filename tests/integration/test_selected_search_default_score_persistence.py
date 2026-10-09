"""Scheduled saved searches without role profiles preserve legacy match state."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.matching import build_explanation
from services.discovery.scheduler import schedule_due_search_profiles
from services.discovery.search_profiles import save_search_profile
from services.discovery.search_semantics import evaluate_search
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class SelectedSearchDefaultScorePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-selected-default-score-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'default-score.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.profile = self.store.profile("fixture")
        self.source_id = configure_source(
            self.store, "selected-default-score", provider="remoteok", jitter_seconds=0,
        )
        self.endpoint = "https://synthetic-source.example/jobs"
        self._save_search()
        self._seed_jobs()

    def _save_search(self):
        save_search_profile(self.store, {
            "id": "default-score-search",
            "profile_id": "fixture",
            "name": "Unconfigured Systems Search",
            "enabled": True,
            "include_titles": ["Systems Administrator"],
            "exclude_titles": [],
            "required_keywords": ["Linux"],
            "optional_keywords": [],
            "excluded_keywords": ["temporary contract"],
            "exact_phrases": [],
            "technology_aliases": {},
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [self.source_id],
            "scheduler": {"enabled": True, "interval_seconds": 300},
        })

    def _seed_jobs(self):
        jobs = (
            ("eligible-job", "Systems Administrator", "Linux systems administration"),
            ("excluded-job", "Account Executive", "Linux temporary contract sales"),
        )
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="synthetic-employer", name="Synthetic Infrastructure",
                domain="synthetic.example",
            ))
            for job_id, title, description in jobs:
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id="synthetic-employer",
                    requisition=f"req-{job_id}", title=title,
                    description=description, skills=["Linux"], location="Remote",
                    remote_type="REMOTE",
                    attributes={"source_id": self.source_id,
                                "source_endpoint": self.endpoint},
                ))
                connection.execute(m.postings.insert().values(
                    id=f"posting-{job_id}", job_id=job_id, source=self.endpoint,
                    source_job_id=job_id, source_url=self.endpoint,
                    application_url=f"https://synthetic.example/jobs/{job_id}",
                    raw={"synthetic": True}, fetched_at="2026-10-09T00:00:00Z",
                ))

    def _criteria(self):
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.search_profiles.c.criteria).where(
                m.search_profiles.c.id == "default-score-search",
            )).scalar_one()
        return deepcopy(row)

    def _hard_eligibility(self, criteria):
        with self.store.engine.connect() as connection:
            jobs = [dict(row) for row in connection.execute(
                select(m.jobs).order_by(m.jobs.c.id),
            ).mappings()]
        return {
            job["id"]: evaluate_search(criteria, job)["matched"]
            for job in jobs
        }

    def _run_scheduled_search(self, at):
        scheduled = schedule_due_search_profiles(self.store, at)
        self.assertEqual([row["search_profile_id"] for row in scheduled["scheduled"]],
                         ["default-score-search"])
        operation_id = scheduled["scheduled"][0]["operation_id"]
        environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
        }
        with patch("workers.tasks.collect_source"), \
                patch("workers.tasks._record_supported_collection_candidates"), \
                patch.dict(os.environ, environment):
            result = execute_operation(operation_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        return operation_id

    def _match(self, job_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.matches).where(
                m.matches.c.job_id == job_id,
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == self.profile["version"],
            )).mappings().one())

    def _job(self, job_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.jobs).where(
                m.jobs.c.id == job_id,
            )).mappings().one())

    def _match_history(self):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(
                m.audit.c.id, m.audit.c.action, m.audit.c.payload, m.audit.c.timestamp,
            ).where(
                m.audit.c.entity_id == "fixture",
                m.audit.c.action.in_(["JOB_DISCOVERED", "MATCH_SCORED"]),
            ).order_by(m.audit.c.id)).mappings()]

    def test_scheduled_search_without_role_profile_preserves_legacy_persisted_match(self):
        criteria_before = self._criteria()
        self.assertNotIn("role_scoring_profile", criteria_before)
        eligibility_before = self._hard_eligibility(criteria_before)
        self.assertEqual(eligibility_before, {
            "eligible-job": True,
            "excluded-job": False,
        })

        first_at = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        first_operation_id = self._run_scheduled_search(first_at)
        first_match = self._match("eligible-job")
        legacy = build_explanation(self._job("eligible-job"), self.profile)
        self.assertEqual(first_match["score"], legacy["score"])
        self.assertEqual(first_match["missing"], legacy["missing"])
        for key, value in legacy["components"].items():
            self.assertEqual(first_match["components"][key], value)
        self.assertEqual(first_match["components"]["scoring_profile"]["selection"],
                         "LEGACY_DEFAULT")
        self.assertNotIn("excluded-job", {
            row["job_id"] for row in self._all_matches()
        })
        self.assertEqual(self._hard_eligibility(self._criteria()), eligibility_before)

        history_after_first = self._match_history()
        self.assertEqual({row["action"] for row in history_after_first}, {
            "JOB_DISCOVERED", "MATCH_SCORED",
        })

        second_operation_id = self._run_scheduled_search(
            first_at + timedelta(seconds=301),
        )
        self.assertNotEqual(first_operation_id, second_operation_id)
        self.assertEqual(self._match("eligible-job"), first_match)
        self.assertEqual(self._match_history(), history_after_first)
        self.assertEqual(self._criteria(), criteria_before)
        self.assertEqual(self._hard_eligibility(self._criteria()), eligibility_before)
        self.assertEqual({row["job_id"] for row in self._all_matches()}, {"eligible-job"})

    def _all_matches(self):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.matches).where(
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == self.profile["version"],
            )).mappings()]


if __name__ == "__main__":
    unittest.main()
