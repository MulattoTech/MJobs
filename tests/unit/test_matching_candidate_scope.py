"""Caller-scoped matching preserves current profile and unrelated match history."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.discovery import matching
from tests.integration.test_spine import PROFILE


class MatchingCandidateScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-matching-scope-")
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'matching.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile(dict(PROFILE))
        self.version = self.store.profile("fixture")["version"]
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "scope-company", "name": "Scope Example", "domain": "scope.example",
            })
            for job_id, title in (
                ("candidate-a", "Systems Administrator"),
                ("candidate-b", "Infrastructure Engineer"),
                ("not-selected", "Sales Representative"),
            ):
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": "scope-company", "requisition": job_id,
                    "title": title, "description": "Manage local systems.", "skills": [],
                    "location": "Remote", "remote_type": "REMOTE", "attributes": {},
                })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _match(self, job_id):
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.matches).where(
                m.matches.c.job_id == job_id,
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == self.version,
            )).mappings().one_or_none()
        return dict(row) if row is not None else None

    def test_candidate_ids_scope_scoring_without_hiding_existing_matches(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.matches, {
                "id": identity("not-selected", "fixture", self.version),
                "job_id": "not-selected", "profile_id": "fixture",
                "profile_version": self.version, "score": 73.0,
                "missing": ["preserve this history"], "components": {"origin": "prior-match"},
            })
        prior = self._match("not-selected")

        with patch("services.discovery.matching.build_explanation", wraps=matching.build_explanation) as build:
            matching.recalculate(
                self.store, "fixture", candidate_job_ids=["candidate-b", "candidate-a", "candidate-a"]
            )

        self.assertEqual([call.args[0]["id"] for call in build.call_args_list], ["candidate-a", "candidate-b"])
        self.assertIsNotNone(self._match("candidate-a"))
        self.assertIsNotNone(self._match("candidate-b"))
        self.assertEqual(self._match("not-selected"), prior)

    def test_omitted_candidate_ids_keep_full_rescore_behavior(self):
        with patch("services.discovery.matching.build_explanation", wraps=matching.build_explanation) as build:
            matching.recalculate(self.store, "fixture")

        self.assertEqual(
            [call.args[0]["id"] for call in build.call_args_list],
            ["candidate-a", "candidate-b", "not-selected"],
        )

    def test_candidate_scope_keeps_profile_version_guard(self):
        with self.assertRaises(NoResultFound):
            matching.recalculate(
                self.store, "fixture", profile_version="stale-version",
                candidate_job_ids=["candidate-a"],
            )

        self.assertIsNone(self._match("candidate-a"))

    def test_candidate_ids_must_be_bounded_non_empty_strings(self):
        for values in ("candidate-a", [""], ["x" * 129], ["candidate-a", None], ["a"] * 10_001):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    matching.recalculate(self.store, "fixture", candidate_job_ids=values)


if __name__ == "__main__":
    unittest.main()
