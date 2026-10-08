import unittest

from services.discovery.cheap_prefilter import prefilter_postings


class CheapPrefilterTests(unittest.TestCase):
    def test_title_and_keyword_rules_select_candidate_rows_without_mutating_postings(self):
        systems = {
            "id": "1",
            "title": "Systems Administrator",
            "company": "Northwind",
            "description": "Manage PowerShell and Linux systems.",
            "skills": ["PowerShell"],
            "raw": {"provider": "greenhouse", "id": "source-1"},
        }
        senior = {
            "id": "2",
            "title": "Senior Systems Administrator",
            "description": "PowerShell Linux",
            "raw": {"provider": "greenhouse", "id": "source-2"},
        }
        sales = {
            "id": "3",
            "title": "Sales Representative",
            "description": "PowerShell Linux",
            "raw": {"provider": "greenhouse", "id": "source-3"},
        }
        no_linux = {
            "id": "4",
            "title": "Systems Administrator",
            "description": "Manage PowerShell systems.",
            "raw": {"provider": "greenhouse", "id": "source-4"},
        }
        originals = [systems, senior, sales, no_linux]
        snapshots = [dict(row, raw=dict(row["raw"])) for row in originals]

        result = prefilter_postings(
            originals,
            {
                "include_titles": ["Systems Administrator", "Infrastructure"],
                "exclude_titles": ["Senior"],
                "required_keywords": ["PowerShell", "Linux"],
                "excluded_keywords": ["commission"],
            },
        )

        self.assertEqual(result["candidates"], [systems])
        self.assertEqual(result["rejected"], [senior, sales, no_linux])
        self.assertEqual(result["deferred"], [])
        self.assertEqual(result["stage_counts"], {"captured": 4, "prefiltered": 1})
        self.assertEqual(originals, snapshots)
        self.assertIs(result["candidates"][0], systems)
        self.assertIs(result["rejected"][0], senior)

    def test_optional_keywords_do_not_exclude_rows_and_no_filters_keep_all(self):
        rows = [
            {"id": "a", "title": "Support Analyst", "description": ""},
            {"id": "b", "title": "Operations Analyst", "description": ""},
        ]

        result = prefilter_postings(rows, {"optional_keywords": ["Azure"]})

        self.assertEqual(result["candidates"], rows)
        self.assertEqual(result["rejected"], [])
        self.assertEqual(result["deferred"], [])
        self.assertEqual(result["stage_counts"], {"captured": 2, "prefiltered": 2})

    def test_keyword_aliases_use_the_existing_search_profile_shape(self):
        row = {"id": "alias", "title": "Cloud Administrator", "description": "M365 support"}

        result = prefilter_postings(
            [row],
            {
                "required_keywords": ["Microsoft 365"],
                "technology_aliases": {"Microsoft 365": ["M365", "Office 365"]},
            },
        )

        self.assertEqual(result["candidates"], [row])
        self.assertEqual(result["rejected"], [])

    def test_candidate_limit_defers_plausible_rows_without_dropping_them(self):
        rows = [
            {"id": "first", "title": "Systems Administrator", "description": ""},
            {"id": "second", "title": "Infrastructure Engineer", "description": ""},
            {"id": "third", "title": "Systems Engineer", "description": ""},
            {"id": "unrelated", "title": "Account Executive", "description": ""},
        ]

        result = prefilter_postings(
            rows,
            {"include_titles": ["Systems", "Infrastructure"]},
            max_candidates=2,
        )

        self.assertEqual(result["candidates"], rows[:2])
        self.assertEqual(result["deferred"], [rows[2]])
        self.assertEqual(result["rejected"], [rows[3]])
        self.assertEqual(
            result["stage_counts"],
            {"captured": 4, "prefiltered": 2},
        )
        self.assertCountEqual(
            result["candidates"] + result["deferred"] + result["rejected"],
            rows,
        )

    def test_candidate_limit_is_deterministic(self):
        rows = [
            {"id": str(index), "title": "Systems Administrator", "description": ""}
            for index in range(5)
        ]

        first = prefilter_postings(rows, {"include_titles": ["Systems"]}, max_candidates=3)
        second = prefilter_postings(rows, {"include_titles": ["Systems"]}, max_candidates=3)

        self.assertEqual(first, second)
        self.assertEqual([row["id"] for row in first["deferred"]], ["3", "4"])

    def test_default_candidate_limit_is_bounded_and_excess_rows_are_deferred(self):
        rows = [
            {"id": str(index), "title": "Systems Administrator", "description": ""}
            for index in range(501)
        ]

        result = prefilter_postings(rows, {})

        self.assertEqual(len(result["candidates"]), 500)
        self.assertEqual(result["deferred"], [rows[-1]])
        self.assertEqual(len(result["rejected"]), 0)
        self.assertEqual(result["stage_counts"], {"captured": 501, "prefiltered": 500})

    def test_overlong_text_is_retained_as_uncertain_instead_of_scanning_unbounded_text(self):
        row = {
            "id": "long-description",
            "title": "Systems Administrator",
            "description": "x" * 50_001,
        }

        result = prefilter_postings(
            [row], {"required_keywords": ["Linux"]}, max_candidates=1
        )

        self.assertEqual(result["candidates"], [row])
        self.assertEqual(result["rejected"], [])

    def test_invalid_or_unbounded_limits_fail_closed(self):
        rows = [{"id": "1", "title": "Systems Administrator", "description": ""}]

        for value in (0, -1, True, 10_001, 1.5):
            with self.subTest(max_candidates=value):
                with self.assertRaises(ValueError):
                    prefilter_postings(rows, {}, max_candidates=value)

    def test_malformed_criteria_fail_instead_of_changing_filter_meaning(self):
        rows = [{"id": "1", "title": "Systems Administrator", "description": ""}]

        for criteria in (
            {"include_titles": "Systems"},
            {"required_keywords": [""]},
            {"excluded_keywords": ["x" * 201]},
            {"technology_aliases": {"PowerShell": "pwsh"}},
        ):
            with self.subTest(criteria=criteria):
                with self.assertRaises(ValueError):
                    prefilter_postings(rows, criteria)


if __name__ == "__main__":
    unittest.main()
