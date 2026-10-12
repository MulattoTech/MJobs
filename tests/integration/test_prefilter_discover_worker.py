"""DISCOVER applies saved-search prefiltering after durable source capture."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from httpx import MockTransport, Response

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.matching import recalculate
from services.discovery.search_profiles import get_search_profile, save_search_profile
from services.discovery.search_history import list_search_history
from services.discovery.sources.collect import collect_source, configure_source
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

    def test_full_saved_criteria_are_applied_to_canonical_fields_before_scoring(self):
        baseline = get_search_profile(self.store, "systems-search")["criteria"]
        cases = [
            ({"remote": "REQUIRED"}, {}, "REMOTE", True),
            ({"remote": "REQUIRED"}, {}, "ONSITE", False),
            ({"employment_types": ["FULL_TIME"]}, {"employment_type": "FULL_TIME"}, "REMOTE", True),
            ({"employment_types": ["FULL_TIME"]}, {}, "REMOTE", False),
            ({"employment_types": ["UNKNOWN"]}, {}, "REMOTE", True),
            ({"countries": ["US"]}, {"country": "US"}, "REMOTE", True),
            ({"countries": ["US"]}, {"country": "CA"}, "REMOTE", False),
            ({"required_skills": ["Linux"]}, {"skill_ids": ["LINUX"]}, "REMOTE", True),
            ({"required_skills": ["Linux"]}, {"skill_ids": []}, "REMOTE", False),
            ({"salary_known_only": True}, {"salary_min": 100000}, "REMOTE", True),
            ({"salary_known_only": True}, {}, "REMOTE", False),
            ({"company_exclude": ["Example"]}, {}, "REMOTE", False),
            ({"exclude_clearance_required": True}, {"clearance": {"level": "SECRET"}}, "REMOTE", False),
        ]
        for case_index, (criteria, attributes, remote_type, admitted) in enumerate(cases):
            with self.subTest(criteria=criteria, attributes=attributes, remote_type=remote_type):
                save_search_profile(self.store, {
                    "id": "systems-search", "profile_id": "fixture", "name": "Systems roles",
                    "include_titles": ["Systems Administrator"], "required_keywords": ["Linux"],
                    "source_ids": [self.source_id], **baseline, **criteria,
                })
                with self.store.engine.begin() as connection:
                    connection.execute(m.jobs.update().where(m.jobs.c.id == "job-match").values(
                        remote_type=remote_type, attributes={
                            "source_id": self.source_id, "source_endpoint": self.endpoint, **attributes,
                        },
                    ))
                before = self._capture_state()
                scored = self._run_worker({
                    "source_id": self.source_id, "search_profile_id": "systems-search",
                    "fixture_case": case_index,
                }, recalculate_mock=Mock())
                self.assertEqual(set(scored.call_args.kwargs["candidate_job_ids"]),
                                 {"job-match"} if admitted else set())
                self.assertEqual(self._capture_state(), before)


class SavedSearchCaptureScoringTests(unittest.TestCase):
    """Exercise ingestion and scoring together, substituting only provider HTTP."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-search-capture-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'worker.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        self.source_id = configure_source(self.store, "search-capture-fixture", jitter_seconds=0)
        self.search = {
            "id": "selected-search", "profile_id": "fixture", "name": "Linux systems",
            "include_titles": ["Systems Administrator"], "exclude_titles": ["Intern"],
            "required_keywords": ["Linux"], "excluded_keywords": ["commission"],
            "source_ids": [self.source_id],
        }
        save_search_profile(self.store, self.search)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _run(self, *, selected=True, profile_ids=None, run_marker=None, other_canonical_source=False):
        from services.discovery import matching

        titles = ["Systems Administrator", "Accountant", "Systems Administrator Intern",
                  "Systems Administrator", "Systems Administrator"]
        descriptions = ["Maintain Linux services.", "Prepare statements.",
                        "Maintain Linux services.", "Maintain Windows services.",
                        "Maintain Linux services on commission."]
        jobs = [{"id": index, "title": title, "content": f"<p>{description}</p>",
                 "location": {"name": "Remote"}, "updated_at": "2026-10-01T00:00:00Z",
                 "absolute_url": f"https://job-boards.greenhouse.io/search-capture-fixture/jobs/{index}"}
                for index, (title, description) in enumerate(zip(titles, descriptions), start=1)]

        def transport(request):
            return Response(200, json={"jobs": jobs, "meta": {"total": len(jobs)}}
                            if request.url.path.endswith("/jobs") else {"name": "Synthetic Employer"})

        def collect(current_store, source_id, profile_id, **kwargs):
            result = collect_source(current_store, source_id, profile_id,
                                    transport=MockTransport(transport), **kwargs)
            if other_canonical_source:
                with current_store.engine.begin() as connection:
                    for row in connection.execute(select(m.jobs.c.id, m.jobs.c.attributes)).mappings():
                        connection.execute(m.jobs.update().where(m.jobs.c.id == row["id"]).values(
                            attributes={**row["attributes"], "source_id": "another-source",
                                        "source_endpoint": "https://other.example/feed"},
                        ))
            return result

        params = {"source_id": self.source_id, "profile_ids": profile_ids or ["fixture"]}
        if run_marker is not None:
            params["fixture_run"] = run_marker
        if selected:
            params["search_profile_id"] = "selected-search"
        operation_id = enqueue_operation(self.store, "DISCOVER", "fixture", params)
        with patch("workers.tasks.collect_source", side_effect=collect), \
                patch("services.discovery.matching.build_explanation", wraps=matching.build_explanation) as scored, \
                patch.dict(os.environ, {"MJOBS_DATABASE_URL": self.store.url,
                                        "MJOBS_OUTPUT_DIR": str(self.root),
                                        "MJOBS_OPERATION_MODE": "FIXTURE"}):
            result = execute_operation(operation_id)
        self.assertEqual(result["status"], "COMPLETED", result)
        return [(call.args[0]["requisition"], call.args[1]["id"]) for call in scored.call_args_list]

    def _matches(self, profile_id):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.matches).where(
                m.matches.c.profile_id == profile_id)).mappings()]

    def test_selected_search_filters_before_any_profile_scoring_and_survives_reopen(self):
        scored = self._run()
        self.assertEqual(scored, [("1", "fixture")])
        with self.store.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(m.postings)).all()), 5)
            self.assertEqual(len(connection.execute(select(m.posting_value_candidates)).all()), 5)
            self.assertEqual(len(connection.execute(select(m.jobs)).all()), 5)
        before = self._matches("fixture")
        self.assertEqual(len(before), 1)
        self.assertEqual(self._matches("other"), [])
        reopened = Store(self.store.url)
        try:
            with reopened.engine.connect() as connection:
                self.assertEqual([dict(row) for row in connection.execute(select(m.matches)).mappings()], before)
            history = list_search_history(reopened, "fixture")["runs"][0]
            self.assertEqual(history["filters"]["include_titles"], ["Systems Administrator"])
            self.assertEqual(history["results"], 5)
        finally:
            reopened.engine.dispose()

    def test_unselected_collection_preserves_eager_scoring(self):
        scored = self._run(selected=False)
        self.assertEqual(set(scored), {(str(index), "fixture") for index in range(1, 6)})
        self.assertEqual(len(self._matches("fixture")), 5)

    def test_posting_lineage_scopes_candidates_when_canonical_winner_is_another_source(self):
        self.assertEqual(self._run(other_canonical_source=True), [("1", "fixture")])
        self.assertEqual(len(self._matches("fixture")), 1)

    def test_unknown_employment_is_not_silently_treated_as_full_time(self):
        save_search_profile(self.store, {**self.search, "employment_types": ["FULL_TIME"]})
        self.assertEqual(self._run(), [])
        self.assertEqual(self._matches("fixture"), [])
        with self.store.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(m.postings)).all()), 5)
        reopened = Store(self.store.url)
        try:
            history = list_search_history(reopened, "fixture")["runs"][0]
            self.assertEqual(history["filters"]["employment_types"], ["FULL_TIME"])
            self.assertEqual(history["results"], 5)
        finally:
            reopened.engine.dispose()
        save_search_profile(self.store, {**self.search, "employment_types": ["UNKNOWN"]})
        with self.store.engine.begin() as connection:
            connection.execute(m.job_sources.update().where(m.job_sources.c.id == self.source_id).values(
                last_attempt="2020-01-01T00:00:00+00:00",
            ))
        self.assertEqual(self._run(run_marker="edited-unknown"), [("1", "fixture")])

    def test_secondary_profile_keeps_full_recalculation_without_primary_exclusions(self):
        scored = self._run(profile_ids=["fixture", "other"])
        self.assertEqual([job for job, profile in scored if profile == "fixture"], ["1"])
        self.assertEqual({job for job, profile in scored if profile == "other"}, {str(i) for i in range(1, 6)})
        self.assertEqual(len(self._matches("fixture")), 1)
        self.assertEqual(len(self._matches("other")), 5)

    def test_edited_search_preserves_excluded_existing_matches_and_score_history(self):
        self._run(selected=False)
        recalculate(self.store, "fixture")
        with self.store.engine.connect() as connection:
            accountant_id = connection.execute(select(m.jobs.c.id).where(
                m.jobs.c.title == "Accountant")).scalar_one()
            before = dict(connection.execute(select(m.matches).where(
                m.matches.c.job_id == accountant_id)).mappings().one())
            history_before = [dict(row) for row in connection.execute(select(m.audit).where(
                m.audit.c.entity_id == "fixture",
                m.audit.c.payload["job_id"].as_string() == accountant_id)).mappings()]
        self.assertTrue(any(row["action"] == "MATCH_SCORED" for row in history_before))
        # Simulate a saved edit and the next eligible scheduled run.
        save_search_profile(self.store, {**self.search, "include_titles": ["Systems"],
                                        "required_keywords": ["Linux services"]})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source_id)
                               .values(last_attempt=None))
        self.assertEqual(self._run(), [("1", "fixture")])
        with self.store.engine.connect() as connection:
            self.assertEqual(dict(connection.execute(select(m.matches).where(
                m.matches.c.job_id == accountant_id)).mappings().one()), before)
            # Capture may add lifecycle records; it must not rewrite score history.
            self.assertEqual([dict(row) for row in connection.execute(select(m.audit).where(
                m.audit.c.entity_id == "fixture",
                m.audit.c.payload["job_id"].as_string() == accountant_id,
                m.audit.c.action == "MATCH_SCORED")).mappings()],
                [row for row in history_before if row["action"] == "MATCH_SCORED"])

    def test_empty_candidate_set_does_not_fall_back_to_full_primary_scoring(self):
        self._run(selected=False)
        recalculate(self.store, "fixture")
        before = self._matches("fixture")
        with self.store.engine.connect() as connection:
            history_before = [dict(row) for row in connection.execute(select(m.audit).where(
                m.audit.c.action == "MATCH_SCORED")).mappings()]
        self.assertTrue(history_before)
        save_search_profile(self.store, {**self.search, "include_titles": ["Astronaut"]})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == self.source_id)
                               .values(last_attempt=None))
        self.assertEqual(self._run(), [])
        self.assertEqual(self._matches("fixture"), before)
        with self.store.engine.connect() as connection:
            self.assertEqual([dict(row) for row in connection.execute(select(m.audit).where(
                m.audit.c.action == "MATCH_SCORED")).mappings()], history_before)
            self.assertEqual(len(connection.execute(select(m.postings)).all()), 5)


if __name__ == "__main__":
    unittest.main()
