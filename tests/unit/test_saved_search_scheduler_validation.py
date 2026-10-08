"""Service-level validation for saved-search scheduler configuration."""
import unittest

from services.discovery.search_profiles import _clean_search_scheduler


class SavedSearchSchedulerValidationTests(unittest.TestCase):
    def test_accepts_exact_scheduler_shape_and_returns_normalized_copy(self):
        value = {"enabled": True, "interval_seconds": 3600}

        cleaned = _clean_search_scheduler(value)

        self.assertEqual(cleaned, value)
        self.assertIsNot(cleaned, value)

    def test_enabled_must_be_a_boolean(self):
        for enabled in (0, 1, "true", None, [], {}):
            with self.subTest(enabled=enabled):
                with self.assertRaisesRegex(ValueError, "enabled must be boolean"):
                    _clean_search_scheduler({"enabled": enabled, "interval_seconds": 300})

    def test_interval_accepts_inclusive_bounds(self):
        for interval in (300, 604800):
            with self.subTest(interval=interval):
                self.assertEqual(
                    _clean_search_scheduler({"enabled": False, "interval_seconds": interval}),
                    {"enabled": False, "interval_seconds": interval},
                )

    def test_interval_rejects_out_of_range_values_and_non_integer_types(self):
        for interval in (299, 604801, 300.0, 604800.0, True, False):
            with self.subTest(interval=interval):
                with self.assertRaisesRegex(ValueError, "interval must be an integer"):
                    _clean_search_scheduler({"enabled": True, "interval_seconds": interval})

    def test_requires_exactly_enabled_and_interval_seconds_keys(self):
        for value in (
            {"enabled": True},
            {"interval_seconds": 300},
            {"enabled": True, "interval_seconds": 300, "last_run": None},
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "include enabled and interval_seconds"):
                    _clean_search_scheduler(value)


if __name__ == "__main__":
    unittest.main()
