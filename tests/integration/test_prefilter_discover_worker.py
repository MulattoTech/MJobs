"""DISCOVER applies saved-search prefiltering after durable source capture."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.matching import recalculate
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation
from workers.tasks import execute_operation


class DiscoverWorkerPrefilterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-discover-prefilter-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'worker.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        self.source_id = configure_source(
            self.store, "prefilter-worker-fixture", provider="remoteok", jitter_seconds=0
        )
        self.endpoint = "https://remoteok.com/api"
        save_search_profile(self.store, {
            "id": "systems-search",
            "profile_id": "fixture",
            "name": "Systems roles",
            "include_titles": ["Systems Administrator"],
            "required_keywords": ["Linux"],
            "source_ids": [self.source_id],
        })
        self._seed_captured_postings()
        recalculate(self.store, "fixture")

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _seed_captured_postings(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="employer", name="Example Employer", domain="employer.example"
            ))
            rows = (
                ("job-match", "Systems Administrator", "Maintain Linux services."),
                ("job-excluded", "Account Executive", "Manage enterprise sales."),
                ("job-other-source", "Systems Administrator", "Maintain Linux services."),
            )
            for job_id, title, description in rows:
                source_id = "another-source" if job_id == "job-other-source" else self.source_id
                source_endpoint = "https://other.example/feed" if job_id == "job-other-source" else self.endpoint
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id="employer", requisition=job_id,
                    title=title, description=description, skills=[], location="Remote",
                    remote_type="REMOTE", attributes={
                        "source_id": source_id,
                        "source_endpoint": source_endpoint,
                    },
                ))
                connection.execute(m.postings.insert().values(
                    id=f"posting-{job_id}", job_id=job_id, source=source_endpoint,
                    source_job_id=job_id, source_url=source_endpoint,
                    application_url=f"https://employer.example/jobs/{job_id}",
                    raw={"provider_id": job_id}, fetched_at="2026-10-08T00:00:00Z",
                ))

    def _capture_state(self):
        with self.store.engine.connect() as connection:
            posting_ids = list(connection.execute(
                select(m.postings.c.id).order_by(m.postings.c.id)
            ).scalars())
            matches = [dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.job_id)
            ).mappings()]
            score_history = [dict(row) for row in connection.execute(
                select(m.audit.c.entity_id, m.audit.c.payload, m.audit.c.timestamp).where(
                    m.audit.c.action == "MATCH_SCORED"
                ).order_by(m.audit.c.entity_id)
            ).mappings()]
        return posting_ids, matches, score_history

    def _run_worker(self, params, *, recalculate_mock):
        operation_id = enqueue_operation(self.store, "DISCOVER", "fixture", params)
        environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:8090",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
        }
        with patch("workers.tasks.collect_source"), \
                patch("workers.tasks._record_supported_collection_candidates"), \
                patch("workers.tasks.recalculate", recalculate_mock), \
                patch.dict(os.environ, environment):
            result = execute_operation(operation_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        return recalculate_mock

    def test_saved_search_rescores_only_matching_source_candidates_and_preserves_history(self):
        before = self._capture_state()
        recalculate_mock = Mock()

        self._run_worker({
            "source_id": self.source_id,
            "profile_ids": ["fixture", "other"],
            "search_profile_id": "systems-search",
        }, recalculate_mock=recalculate_mock)

        self.assertEqual(recalculate_mock.call_count, 2)
        primary_args, primary_kwargs = recalculate_mock.call_args_list[0]
        self.assertEqual(primary_args[1], "fixture")
        self.assertEqual(set(primary_kwargs.get("candidate_job_ids", ())), {"job-match"})
        secondary_args, secondary_kwargs = recalculate_mock.call_args_list[1]
        self.assertEqual(secondary_args[1], "other")
        self.assertNotIn("candidate_job_ids", secondary_kwargs)
        self.assertEqual(self._capture_state(), before)

    def test_rescore_only_keeps_the_existing_full_recalculation_path(self):
        recalculate_mock = Mock()

        self._run_worker({"rescore_only": True}, recalculate_mock=recalculate_mock)

        recalculate_mock.assert_called_once()
        _, kwargs = recalculate_mock.call_args
        self.assertNotIn("candidate_job_ids", kwargs)


if __name__ == "__main__":
    unittest.main()
