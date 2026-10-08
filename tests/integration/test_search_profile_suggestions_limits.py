"""Boundary checks for the search-profile suggestions API query parameters."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class SearchProfileSuggestionsLimitsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-suggestion-limits-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        from apps.api.search_profile_suggestions import create_search_profile_suggestions_router
        self.client.app.include_router(create_search_profile_suggestions_router(self.store))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_current_title_query_accepts_limit_and_rejects_values_over_limit(self):
        boundary = self.client.get(
            "/profiles/fixture/search-profile-suggestions",
            params={"current_title": "x" * 500},
        )
        self.assertEqual(boundary.status_code, 200, boundary.text)
        self.assertEqual(boundary.json()["current_title"], "x" * 500)

        oversized = self.client.get(
            "/profiles/fixture/search-profile-suggestions",
            params={"current_title": "x" * 501},
        )
        self.assertEqual(oversized.status_code, 422, oversized.text)


if __name__ == "__main__":
    unittest.main()
