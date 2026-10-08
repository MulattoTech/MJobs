"""Profile-versioned title suggestions from existing strong match categories."""
from pathlib import Path
import tempfile
import unittest

from packages.persistence import models as m
from packages.persistence.store import Store
from services.learning.search_profile_suggestions import suggest_search_profile_titles
from tests.integration.test_spine import PROFILE


class SearchProfileSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-title-suggestions-")
        self.store = Store(
            f"sqlite:///{(Path(self.directory.name) / 'jobs.db').as_posix()}"
        )
        self.store.migrate()
        self.store.save_profile(dict(PROFILE))
        self._next_job = 0

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _job(self, title):
        self._next_job += 1
        job_id = f"job-{self._next_job}"
        company_id = f"company-{self._next_job}"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name="Synthetic Company", domain="example.test",
                parent_company_id=None, relationship_type=None,
            ))
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id=company_id, requisition=job_id, title=title,
                description="", skills=[], location="", remote_type="UNKNOWN",
                attributes={},
            ))
        return job_id

    def _match(self, title, score, *, profile_id="fixture", profile_version=None):
        job_id = self._job(title)
        profile_version = profile_version or self.store.profile(profile_id)["version"]
        with self.store.engine.begin() as connection:
            connection.execute(m.matches.insert().values(
                id=f"match-{job_id}", job_id=job_id, profile_id=profile_id,
                profile_version=profile_version, score=score, missing=[], components={},
            ))
        return job_id

    def test_uses_only_current_profile_version_and_existing_strong_categories(self):
        prior_version = self.store.profile("fixture")["version"]
        changed = dict(PROFILE)
        changed["name"] = "Updated profile"
        self.store.save_profile(changed)
        self._match("Stale Infrastructure Engineer", 99, profile_version=prior_version)
        self._match("Infrastructure Support Engineer", 75)
        self._match("Platform Administrator", 94)
        self._match("Review Only Engineer", 74)

        other = dict(PROFILE)
        other["id"] = "other-profile"
        self.store.save_profile(other)
        self._match("Other Profile Engineer", 100, profile_id="other-profile")

        self.assertEqual(
            suggest_search_profile_titles(self.store, "fixture"),
            [
                {"title": "Infrastructure Support Engineer", "strong_match_count": 1,
                 "reason": "Observed in 1 current strong match."},
                {"title": "Platform Administrator", "strong_match_count": 1,
                 "reason": "Observed in 1 current strong match."},
            ],
        )

    def test_aggregates_casefold_duplicates_and_sorts_deterministically(self):
        self._match("Platform Administrator", 80)
        self._match("infrastructure support engineer", 100)
        self._match("Infrastructure Support Engineer", 75)
        self._match("IT Operations Engineer", 85)

        self.assertEqual(
            suggest_search_profile_titles(self.store, "fixture"),
            [
                {"title": "Infrastructure Support Engineer", "strong_match_count": 2,
                 "reason": "Observed in 2 current strong matches."},
                {"title": "IT Operations Engineer", "strong_match_count": 1,
                 "reason": "Observed in 1 current strong match."},
                {"title": "Platform Administrator", "strong_match_count": 1,
                 "reason": "Observed in 1 current strong match."},
            ],
        )

    def test_excludes_the_current_search_title(self):
        self._match("System Administrator", 75)
        self._match("Platform Administrator", 75)

        self.assertEqual(
            suggest_search_profile_titles(
                self.store, "fixture", current_title=" system administrator "
            ),
            [{"title": "Platform Administrator", "strong_match_count": 1,
              "reason": "Observed in 1 current strong match."}],
        )

    def test_unknown_profile_has_no_suggestions(self):
        self.assertEqual(suggest_search_profile_titles(self.store, "missing"), [])

    def test_jobs_without_a_persisted_match_score_are_not_suggested(self):
        self._job("Unscored Infrastructure Engineer")

        self.assertEqual(suggest_search_profile_titles(self.store, "fixture"), [])

    def test_sorts_by_match_count_then_title_and_caps_at_twenty(self):
        for number in range(21):
            self._match(f"Role {number:02}", 75)
        self._match("Role 20", 75)

        suggestions = suggest_search_profile_titles(self.store, "fixture")

        self.assertEqual(len(suggestions), 20)
        self.assertEqual(
            suggestions[0],
            {"title": "Role 20", "strong_match_count": 2,
             "reason": "Observed in 2 current strong matches."},
        )
        self.assertEqual(suggestions[1]["title"], "Role 00")
        self.assertEqual(suggestions[-1]["title"], "Role 18")
        self.assertNotIn("Role 19", [item["title"] for item in suggestions])


if __name__ == "__main__":
    unittest.main()
