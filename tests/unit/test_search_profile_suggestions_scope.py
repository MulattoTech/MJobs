"""Selected-search scope and current-evidence rules for title suggestions."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.learning.search_profile_suggestions import suggest_search_profile_titles

PROFILE = {
    "id": "fixture",
    "name": "Synthetic Profile",
    "email": "synthetic@example.test",
    "answers": [],
    "facts": [],
}


class SearchProfileSuggestionScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-title-suggestion-scope-")
        self.store = Store(
            f"sqlite:///{(Path(self.directory.name) / 'jobs.db').as_posix()}"
        )
        self.store.migrate()
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other-profile"})
        self.next_job = 0

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _match(self, title, score, *, profile_id="fixture", profile_version=None):
        self.next_job += 1
        job_id = f"scope-job-{self.next_job}"
        company_id = f"scope-company-{self.next_job}"
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
            connection.execute(m.matches.insert().values(
                id=f"scope-match-{job_id}", job_id=job_id, profile_id=profile_id,
                profile_version=profile_version or self.store.profile(profile_id)["version"],
                score=score, missing=[], components={},
            ))

    def test_excludes_selected_search_titles_and_only_uses_current_strong_evidence(self):
        previous_version = self.store.profile("fixture")["version"]
        updated_profile = dict(PROFILE)
        updated_profile["name"] = "Current profile"
        self.store.save_profile(updated_profile)

        self._match("Platform Administrator", 99, profile_version=previous_version)
        self._match("System Administrator", 95)
        self._match("Infrastructure Support Engineer", 75)
        self._match("Infrastructure Support Engineer", 90)
        self._match("Review Only Engineer", 74)
        self._match("Foreign Profile Engineer", 99, profile_id="other-profile")

        self.assertEqual(
            suggest_search_profile_titles(
                self.store,
                "fixture",
                current_title=" system administrator ",
                excluded_titles=[
                    " PLATFORM ADMINISTRATOR ",
                    "platform administrator",
                    "  Platform Administrator  ",
                ],
            ),
            [{
                "title": "Infrastructure Support Engineer",
                "strong_match_count": 2,
                "reason": "Observed in 2 current strong matches.",
            }],
        )

    def test_duplicate_selected_search_exclusions_do_not_suppress_other_candidate_matches(self):
        self._match("System Administrator", 95)
        self._match("Infrastructure Support Engineer", 75)
        self._match("Infrastructure Support Engineer", 90)

        self.assertEqual(
            suggest_search_profile_titles(
                self.store,
                "fixture",
                excluded_titles=[
                    " SYSTEM ADMINISTRATOR ",
                    "system administrator",
                    "  System Administrator  ",
                ],
            ),
            [{
                "title": "Infrastructure Support Engineer",
                "strong_match_count": 2,
                "reason": "Observed in 2 current strong matches.",
            }],
        )

    def test_search_including_all_matched_titles_returns_empty_without_changing_evidence(self):
        selected_search_include_titles = [
            "System Administrator",
            "Infrastructure Support Engineer",
            "Platform Administrator",
        ]
        self._match("System Administrator", 95)
        self._match("Infrastructure Support Engineer", 75)
        self._match("Platform Administrator", 90)

        def persisted_evidence():
            with self.store.engine.connect() as connection:
                return connection.execute(
                    select(
                        m.matches.c.job_id,
                        m.matches.c.profile_version,
                        m.matches.c.score,
                    ).where(m.matches.c.profile_id == "fixture")
                    .order_by(m.matches.c.job_id)
                ).all()

        evidence_before = persisted_evidence()
        self.assertEqual(len(evidence_before), 3)

        suggestions = suggest_search_profile_titles(
            self.store,
            "fixture",
            excluded_titles=selected_search_include_titles,
        )

        self.assertEqual(suggestions, [])
        self.assertEqual(persisted_evidence(), evidence_before)


if __name__ == "__main__":
    unittest.main()
