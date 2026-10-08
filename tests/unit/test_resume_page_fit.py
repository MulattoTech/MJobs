import unittest

from services.documents.page_fit import fit_to_page_target


class ResumePageFitTests(unittest.TestCase):
    def setUp(self):
        self.facts = [
            {"fact_id": "summary", "kind": "summary", "text": "Protected summary",
             "approved": True, "protected": True, "optional_bullet": False, "rank": 100},
            {"fact_id": "keep", "kind": "responsibility", "text": "Strong relevant outcome",
             "approved": True, "protected": False, "optional_bullet": True, "rank": 10},
            {"fact_id": "trim-first", "kind": "responsibility", "text": "Less relevant outcome",
             "approved": True, "protected": False, "optional_bullet": True, "rank": 1},
            {"fact_id": "selected", "kind": "project", "text": "User selected project",
             "approved": True, "protected": False, "optional_bullet": True,
             "explicitly_selected": True, "rank": 0},
        ]

    def test_trims_only_lowest_rank_eligible_bullet_and_records_reason(self):
        measured = []

        def measure(rows):
            ids = [row["fact_id"] for row in rows]
            measured.append(ids)
            return 2 if "trim-first" in ids else 1

        result = fit_to_page_target(self.facts, page_target=1, measure_page_count=measure)

        self.assertEqual([row["fact_id"] for row in result.selected_facts],
                         ["summary", "keep", "selected"])
        self.assertEqual(result.omitted_facts, ({
            "fact_id": "trim-first", "reason": "PAGE_TARGET_FIT_LOWEST_RETENTION_RANK"
        },))
        self.assertEqual(result.page_count, 1)
        self.assertTrue(result.target_met)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(measured), 2)

    def test_equal_rank_removal_is_stable_and_never_removes_selected_or_protected(self):
        facts = [dict(row, rank=1) if row["fact_id"] in {"keep", "trim-first"} else row
                 for row in self.facts]
        result = fit_to_page_target(
            facts, page_target=1,
            measure_page_count=lambda rows: 2 if len(rows) > 3 else 1,
        )

        self.assertEqual([row["fact_id"] for row in result.selected_facts],
                         ["summary", "keep", "selected"])
        self.assertEqual([row["fact_id"] for row in result.selected_facts[:1]], ["summary"])

    def test_reports_unmet_target_when_attempt_bound_is_reached(self):
        calls = []
        result = fit_to_page_target(
            self.facts, page_target=1, max_attempts=2,
            measure_page_count=lambda rows: calls.append(len(rows)) or 3,
        )

        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.page_count, 3)
        self.assertFalse(result.target_met)
        self.assertEqual(result.selected_facts, tuple(self.facts))
        self.assertEqual(result.omitted_facts, ())
        self.assertEqual(result.reason, "MAX_RENDER_ATTEMPTS_REACHED")

    def test_unavailable_measurement_keeps_all_facts_and_reports_no_guess(self):
        def unavailable(_rows):
            raise OSError("renderer unavailable")

        result = fit_to_page_target(
            self.facts, page_target=1, measure_page_count=unavailable,
        )

        self.assertEqual(len(result.selected_facts), len(self.facts))
        self.assertEqual(result.omitted_facts, ())
        self.assertIsNone(result.page_count)
        self.assertFalse(result.measurement_available)
        self.assertFalse(result.target_met)
        self.assertEqual(result.reason, "PAGE_MEASUREMENT_UNAVAILABLE")

    def test_later_measurement_failure_returns_original_with_its_measured_count(self):
        calls = []

        def measure(_rows):
            calls.append(True)
            if len(calls) == 1:
                return 3
            raise OSError("renderer unavailable")

        result = fit_to_page_target(self.facts, page_target=1, measure_page_count=measure)

        self.assertEqual(result.selected_facts, tuple(self.facts))
        self.assertEqual(result.omitted_facts, ())
        self.assertEqual(result.page_count, 3)
        self.assertEqual(result.attempts, 2)
        self.assertFalse(result.measurement_available)
        self.assertEqual(result.reason, "PAGE_MEASUREMENT_UNAVAILABLE")

    def test_unmet_target_without_removable_bullets_preserves_every_fact(self):
        protected = [dict(row, protected=True, optional_bullet=False) for row in self.facts]
        result = fit_to_page_target(
            protected, page_target=1, measure_page_count=lambda _rows: 2,
        )

        self.assertEqual(result.selected_facts, tuple(protected))
        self.assertEqual(result.omitted_facts, ())
        self.assertEqual(result.page_count, 2)
        self.assertTrue(result.measurement_available)
        self.assertFalse(result.target_met)
        self.assertEqual(result.reason, "NO_ELIGIBLE_BULLETS")

    def test_rejects_invalid_page_target_and_malformed_fact_identity(self):
        with self.assertRaises(ValueError):
            fit_to_page_target(self.facts, page_target=0, measure_page_count=lambda _rows: 1)

        with self.assertRaises(ValueError):
            fit_to_page_target([dict(self.facts[0], fact_id="")], page_target=1,
                               measure_page_count=lambda _rows: 1)


if __name__ == "__main__":
    unittest.main()
