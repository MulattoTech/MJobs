"""Public preview ingress must stay opt-in, exact-host, and same-origin."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store


HOST = "mjobs.m-techlabs.com"
SHA = "a" * 40


def fresh_store():
    store = Store("sqlite:///:memory:")
    store.migrate()
    return store


class PublicHostTests(unittest.TestCase):
    def app(self, **values):
        env = {"MJOBS_PUBLIC_HOST": HOST, "MJOBS_DEPLOY_SHA": SHA, **values}
        with patch.dict(os.environ, env, clear=False):
            return create_app(fresh_store(), "http://127.0.0.1:8090",
                              Path(tempfile.gettempdir()), operation_mode="PERSONAL")

    def test_exact_public_host_serves_revision_without_private_data(self):
        with TestClient(self.app()) as client:
            response = client.get("/__deployment", headers={"Host": HOST})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["commit"], SHA)
            self.assertEqual(response.json()["operation_mode"], "PERSONAL")
            self.assertTrue(response.json()["version"])
            self.assertEqual(client.get("/__deployment",
                             headers={"Host": "other.example.com"}).status_code, 400)

    def test_browser_mutation_accepts_only_configured_https_origin(self):
        with TestClient(self.app()) as client:
            headers = {"Host": HOST, "Sec-Fetch-Site": "same-origin"}
            allowed = client.post("/nonexistent-browser-action",
                                  headers={**headers, "Origin": "https://" + HOST})
            self.assertEqual(allowed.status_code, 404)
            for origin in ("https://evil.example", "http://" + HOST):
                denied = client.post("/nonexistent-browser-action",
                                     headers={**headers, "Origin": origin})
                self.assertEqual(denied.status_code, 403)
            denied_fetch = client.post("/nonexistent-browser-action",
                                      headers={"Host": HOST, "Origin": "https://" + HOST,
                                               "Sec-Fetch-Site": "cross-site"})
            self.assertEqual(denied_fetch.status_code, 403)

    def test_invalid_public_host_and_sha_fail_closed(self):
        for env in ({"MJOBS_PUBLIC_HOST": "*.example.com"},
                    {"MJOBS_PUBLIC_HOST": "https://example.com"},
                    {"MJOBS_PUBLIC_HOST": HOST, "MJOBS_DEPLOY_SHA": "not-a-sha"}):
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(ValueError):
                    create_app(fresh_store(), "http://127.0.0.1:8090",
                               Path(tempfile.gettempdir()), operation_mode="PERSONAL")

    def test_default_configuration_does_not_accept_public_hostname(self):
        with patch.dict(os.environ, {"MJOBS_PUBLIC_HOST": "", "MJOBS_DEPLOY_SHA": ""}):
            app = create_app(fresh_store(), "http://127.0.0.1:8090",
                             Path(tempfile.gettempdir()), operation_mode="PERSONAL")
        with TestClient(app) as client:
            self.assertEqual(client.get("/__deployment", headers={"Host": HOST}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
