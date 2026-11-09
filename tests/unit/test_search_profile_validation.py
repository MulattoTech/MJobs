"""Saved Jobs view preferences must retain explicit, typed user choices."""
import unittest

from services.discovery.search_profiles import _clean_job_filters


class SavedSearchViewPreferenceValidationTests(unittest.TestCase):
    def test_application_priority_and_explicit_visibility_choices_are_accepted(self):
        for dismissed in (False, True):
            for blacklisted in (False, True):
                with self.subTest(dismissed=dismissed, blacklisted=blacklisted):
                    saved = _clean_job_filters({
                        "q": "", "remote_type": "", "provider": "",
                        "sort": "application_priority",
                        "include_dismissed": dismissed,
                        "include_blacklisted": blacklisted,
                    })
                    self.assertEqual(saved["sort"], "application_priority")
                    self.assertIs(saved["include_dismissed"], dismissed)
                    self.assertIs(saved["include_blacklisted"], blacklisted)

    def test_visibility_requires_booleans_without_coercion(self):
        for key in ("include_dismissed", "include_blacklisted"):
            for value in (None, 0, 1, "false", "true", "", [], {}):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, f"Saved Jobs {key} must be boolean"):
                        _clean_job_filters({
                            "q": "", "remote_type": "", "provider": "", "sort": "score",
                            key: value,
                        })

    def test_legacy_filters_do_not_opt_in_to_expanded_visibility(self):
        saved = _clean_job_filters({
            "q": "", "remote_type": "", "provider": "", "sort": "score",
        })
        self.assertNotIn("include_dismissed", saved)
        self.assertNotIn("include_blacklisted", saved)

    def test_unknown_sort_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Saved Jobs sort is invalid"):
            _clean_job_filters({
                "q": "", "remote_type": "", "provider": "", "sort": "automatic_submission",
            })


if __name__ == "__main__":
    unittest.main()
