"""Operation concurrency categories are bounded and independently configurable."""
import os
import unittest
from unittest.mock import patch

from workers.operations import category_limit, limit


class OperationConcurrencyTests(unittest.TestCase):
    def test_each_required_category_has_a_conservative_independent_default(self):
        defaults = {
            "source_collection": 2,
            "company_discovery": 1,
            "ai": 1,
            "document_generation": 1,
            "application_automation": 1,
        }
        names = {
            "source_collection": "MJOBS_SOURCE_COLLECTION_CONCURRENCY",
            "company_discovery": "MJOBS_COMPANY_DISCOVERY_CONCURRENCY",
            "ai": "MJOBS_AI_CONCURRENCY",
            "document_generation": "MJOBS_DOCUMENT_GENERATION_CONCURRENCY",
            "application_automation": "MJOBS_APPLICATION_AUTOMATION_CONCURRENCY",
        }
        with patch.dict(os.environ, {name: "" for name in names.values()}):
            # Empty values must fail closed instead of silently selecting a default.
            for category in defaults:
                with self.subTest(category=category), self.assertRaises(ValueError):
                    category_limit(category)

        with patch.dict(os.environ, {}, clear=True):
            for category, expected in defaults.items():
                with self.subTest(category=category):
                    self.assertEqual(category_limit(category), expected)

    def test_category_limits_are_independent_and_operation_kinds_map_to_them(self):
        with patch.dict(os.environ, {
            "MJOBS_SOURCE_COLLECTION_CONCURRENCY": "3",
            "MJOBS_COMPANY_DISCOVERY_CONCURRENCY": "2",
            "MJOBS_AI_CONCURRENCY": "2",
            "MJOBS_DOCUMENT_GENERATION_CONCURRENCY": "1",
            "MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "1",
        }, clear=True):
            self.assertEqual(limit("DISCOVER"), 3)
            self.assertEqual(limit("COMPANY_DISCOVER"), 2)
            self.assertEqual(limit("COMPANY_SOURCE_REFRESH"), 2)
            self.assertEqual(limit("AI_EVALUATE"), 2)
            self.assertEqual(limit("UPLOADED_RESUME_COMPARE"), 1)
            self.assertEqual(limit("PREPARE"), 1)
            self.assertEqual(limit("APPLY_MOCK"), 1)
            self.assertEqual(category_limit("ai"), 2)

    def test_unknown_categories_and_out_of_range_values_fail_closed(self):
        with self.assertRaises(ValueError):
            category_limit("unknown")
        for category, value in (("source_collection", "5"),
                                ("company_discovery", "3"),
                                ("ai", "3"),
                                ("document_generation", "3"),
                                ("application_automation", "3"),
                                ("source_collection", "0"),
                                ("ai", "many")):
            variable = "MJOBS_" + category.upper() + "_CONCURRENCY"
            with self.subTest(category=category, value=value), \
                    patch.dict(os.environ, {variable: value}, clear=True), \
                    self.assertRaises(ValueError):
                category_limit(category)


if __name__ == "__main__":
    unittest.main()
