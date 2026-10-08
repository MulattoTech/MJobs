import unittest

from services.discovery.cheap_prefilter import (
    MAX_TEXT_FIELD_LENGTH,
    prefilter_postings,
)


class CheapPrefilterBoundaryContractTests(unittest.TestCase):
    def test_text_at_limit_can_be_rejected_but_truncated_text_is_deferred(self):
        matching = {
            "id": "matching",
            "title": "Data Analyst",
            "description": "needle",
        }
        complete_mismatch = {
            "id": "complete-mismatch",
            "title": "Data Analyst",
            "description": "x" * MAX_TEXT_FIELD_LENGTH,
        }
        truncated_mismatch = {
            "id": "truncated-mismatch",
            "title": "Data Analyst",
            "description": "x" * (MAX_TEXT_FIELD_LENGTH + 1),
        }
        rows = [matching, complete_mismatch, truncated_mismatch]

        result = prefilter_postings(
            rows,
            {
                "include_titles": ["Data Analyst"],
                "required_keywords": ["needle"],
            },
            max_candidates=1,
        )

        self.assertEqual(result["candidates"], [matching])
        self.assertEqual(result["rejected"], [complete_mismatch])
        self.assertEqual(result["deferred"], [truncated_mismatch])
        self.assertIs(result["deferred"][0], truncated_mismatch)
        self.assertEqual(result["stage_counts"], {"captured": 3, "prefiltered": 1})


if __name__ == "__main__":
    unittest.main()
