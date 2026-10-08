"""Validation and durable saved-search behavior for application-link availability."""
from pathlib import Path
import tempfile
import unittest

from packages.persistence.store import Store
from services.discovery.search_profiles import _clean_job_filters, list_search_profiles, save_search_profile
from tests.integration.test_spine import PROFILE


class ApplicationLinkSearchFilterValidationTests(unittest.TestCase):
    def test_application_link_status_accepts_only_supported_filter_values(self):
        for status in ("", "AVAILABLE", "UNAVAILABLE"):
            with self.subTest(status=status):
                self.assertEqual(
                    _clean_job_filters({
                        "q": "", "remote_type": "", "provider": "", "sort": "score",
                        "application_link_status": status,
                    })["application_link_status"],
                    status,
                )

    def test_legacy_saved_search_without_application_link_status_remains_unfiltered(self):
        cleaned = _clean_job_filters({
            "q": "", "remote_type": "", "provider": "", "sort": "score",
        })

        self.assertNotIn("application_link_status", cleaned)

    def test_unknown_application_link_status_is_rejected(self):
        for status in ("UNKNOWN", "PROVIDER_SUPPLIED", "available", None, 1):
            with self.subTest(status=status):
                with self.assertRaisesRegex(ValueError, "Saved Jobs application_link_status is invalid"):
                    _clean_job_filters({
                        "q": "", "remote_type": "", "provider": "", "sort": "score",
                        "application_link_status": status,
                    })

    def test_validated_application_link_status_survives_saved_profile_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-app-link-filter-") as directory:
            store = Store(f"sqlite:///{(Path(directory) / 'jobs.db').as_posix()}")
            try:
                store.migrate()
                store.save_profile(dict(PROFILE))
                for status in ("AVAILABLE", "UNAVAILABLE"):
                    identifier = f"application-link-filter-{status.lower()}"
                    save_search_profile(store, {
                        "id": identifier, "profile_id": "fixture",
                        "name": f"Application link is {status}", "enabled": False, "source_ids": [],
                        "job_filters": {
                            "q": "", "remote_type": "", "provider": "", "sort": "score",
                            "application_link_status": status,
                        },
                    })

                saved = {row["id"]: row for row in list_search_profiles(store, "fixture")}
                for status in ("AVAILABLE", "UNAVAILABLE"):
                    identifier = f"application-link-filter-{status.lower()}"
                    self.assertEqual(
                        saved[identifier]["criteria"]["job_filters"]["application_link_status"],
                        status,
                    )
            finally:
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
