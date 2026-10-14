"""Local browser guards protect every API namespace before persisted writes."""
import copy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class LocalOriginSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-origin-")
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'origin.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(copy.deepcopy(PROFILE))
        self.client = self._client()
        self.search = {"id": "private-search", "profile_id": "fixture",
                       "name": "Private synthetic search", "enabled": False}

    def _client(self):
        return TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root,
                                     "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_cross_site_write_without_origin_preserves_search_after_restart(self):
        saved = self.client.put("/search-profiles", json=self.search)
        self.assertEqual(saved.status_code, 200, saved.text)
        rejected = self.client.put(
            "/search-profiles", json={**self.search, "name": "Attacker replacement"},
            headers={"sec-fetch-site": "cross-site"},
        )
        self.assertEqual(rejected.status_code, 403, rejected.text)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.client = self._client()
        reopened = self.client.get("/search-profiles/private-search")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(reopened.json()["name"], "Private synthetic search")

    def test_cross_site_unsafe_methods_are_blocked_before_unknown_routes(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                response = self.client.request(
                    method, "/future-api-namespace", json={},
                    headers={"sec-fetch-site": "cross-site"},
                )
                self.assertEqual(response.status_code, 403, response.text)

    def test_cross_site_read_does_not_disclose_search_or_configuration(self):
        self.assertEqual(self.client.put("/search-profiles", json=self.search).status_code, 200)
        for path in ("/search-profiles/private-search", "/settings", "/retention/policy"):
            with self.subTest(path=path):
                response = self.client.get(path, headers={"sec-fetch-site": "cross-site"})
                self.assertEqual(response.status_code, 403, response.text)
                self.assertNotIn("Private synthetic search", response.text)

    def test_same_origin_browser_and_headerless_local_client_can_save(self):
        for headers in ({}, {"origin": "http://testserver", "sec-fetch-site": "same-origin"}):
            with self.subTest(headers=headers):
                response = self.client.put("/search-profiles", json=self.search, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
        health = self.client.get("/health", headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(health.status_code, 200, health.text)

    def test_conflicting_origin_is_rejected_even_with_same_origin_metadata(self):
        response = self.client.put(
            "/search-profiles", json=self.search,
            headers={"origin": "https://attacker.example", "sec-fetch-site": "same-origin"},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_private_reads_reject_foreign_or_invalid_explicit_origins(self):
        self.assertEqual(self.client.put("/search-profiles", json=self.search).status_code, 200)
        for origin in ("http://testserver:9999", "https://attacker.example", "null", ""):
            for method in ("GET", "HEAD", "OPTIONS"):
                with self.subTest(origin=origin, method=method):
                    response = self.client.request(
                        method, "/search-profiles/private-search",
                        headers={"origin": origin, "sec-fetch-site": "same-origin"},
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn("Private synthetic search", response.text)

    def test_same_site_and_invalid_browser_metadata_cannot_read_private_records(self):
        self.assertEqual(self.client.put("/search-profiles", json=self.search).status_code, 200)
        for fetch_site in ("same-site", "invalid", ""):
            with self.subTest(fetch_site=fetch_site):
                response = self.client.get(
                    "/search-profiles/private-search", headers={"sec-fetch-site": fetch_site},
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertNotIn("Private synthetic search", response.text)

    def test_same_site_mutation_without_origin_preserves_saved_search_after_restart(self):
        self.assertEqual(self.client.put("/search-profiles", json=self.search).status_code, 200)
        response = self.client.put(
            "/search-profiles", json={**self.search, "name": "Sibling origin replacement"},
            headers={"sec-fetch-site": "same-site"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.client = self._client()
        reopened = self.client.get("/search-profiles/private-search")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(reopened.json()["name"], "Private synthetic search")

    def test_direct_navigation_and_same_origin_can_read_but_foreign_shell_is_public(self):
        self.assertEqual(self.client.put("/search-profiles", json=self.search).status_code, 200)
        for headers in ({}, {"sec-fetch-site": "none"},
                        {"origin": "http://testserver", "sec-fetch-site": "same-origin"}):
            with self.subTest(headers=headers):
                response = self.client.get("/search-profiles/private-search", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["name"], "Private synthetic search")
        public = self.client.get("/health", headers={"origin": "http://testserver:9999",
                                                    "sec-fetch-site": "same-site"})
        self.assertEqual(public.status_code, 200, public.text)
        self.assertFalse(public.json()["external_submission_enabled"])
