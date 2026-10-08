"""Integration between profile criteria, cheap capture filtering, and discovery semantics."""
import copy
import unittest

from services.discovery.cheap_prefilter_pipeline import prefilter_search_profile_postings
from services.discovery.search_semantics import filter_jobs


class CheapPrefilterPipelineTests(unittest.TestCase):
    def test_profile_prefilter_preserves_source_rows_before_deterministic_filtering(self):
        profile = {
            "id": "systems-admin",
            "criteria": {
                "include_titles": ["System Administrator"],
                "exclude_titles": [],
                "required_keywords": ["Linux"],
                "optional_keywords": [],
                "excluded_keywords": [],
                "exact_phrases": [],
            },
        }
        source_postings = [
            {"id": "match", "title": "System Administrator", "description": "Linux operations"},
            {"id": "unrelated", "title": "Accountant", "description": "Financial reporting"},
        ]
        original_rows = copy.deepcopy(source_postings)

        result = prefilter_search_profile_postings(source_postings, profile)

        self.assertEqual([row["id"] for row in result["candidates"]], ["match"])
        self.assertEqual([row["id"] for row in result["rejected"]], ["unrelated"])
        self.assertEqual(result["deferred"], [])
        self.assertEqual(result["stage_counts"], {"captured": 2, "prefiltered": 1})
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(result["deferred_count"], 0)
        self.assertEqual(source_postings, original_rows)

        # The existing full deterministic search filter consumes only the cheap-stage candidates.
        accepted = filter_jobs(result["candidates"], profile["criteria"])
        self.assertEqual([row["id"] for row in accepted], ["match"])

    def test_candidate_cap_defers_rows_without_rejecting_or_mutating_them(self):
        profile = {"criteria": {}}
        source_postings = [
            {"id": "first", "title": "System Administrator", "description": "Linux"},
            {"id": "second", "title": "Systems Engineer", "description": "Networking"},
        ]
        original_rows = copy.deepcopy(source_postings)

        result = prefilter_search_profile_postings(source_postings, profile, max_candidates=1)

        self.assertEqual([row["id"] for row in result["candidates"]], ["first"])
        self.assertEqual(result["rejected"], [])
        self.assertEqual([row["id"] for row in result["deferred"]], ["second"])
        self.assertEqual(result["stage_counts"], {"captured": 2, "prefiltered": 1})
        self.assertEqual(source_postings, original_rows)


if __name__ == "__main__":
    unittest.main()
