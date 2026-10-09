"""Blacklisted companies are excluded from search before its result limit."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from apps.api.main import create_app
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from tests.integration.test_spine import PROFILE


class CompanyBlacklistSearchTests(unittest.TestCase):
    def test_blacklist_applies_before_limit_and_can_be_reversed_from_search(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-company-blacklist-search-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        store = Store(f"sqlite:///{(root / 'search.db').as_posix()}")
        self.addCleanup(store.engine.dispose)
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(deepcopy(PROFILE))
        store.save_profile({
            **deepcopy(PROFILE), "id": "other", "email": "other@example.test",
        })
        blacklisted = capture_manual_job(store, {
            "request_id": "blacklist-search-a",
            "title": "A Blacklisted Role", "company": "Avoided Employer",
            "description": "Synthetic role.",
            "reference_url": "https://jobs.example.test/blacklist-a",
            "application_url": "https://apply.example.test/blacklist-a",
            "profile_id": "fixture",
        })["job"]
        visible = capture_manual_job(store, {
            "request_id": "blacklist-search-z",
            "title": "Z Visible Role", "company": "Visible Employer",
            "description": "Synthetic role.",
            "reference_url": "https://jobs.example.test/blacklist-z",
            "application_url": "https://apply.example.test/blacklist-z",
            "profile_id": "fixture",
        })["job"]
        app = create_app(store, "http://127.0.0.1:1", root,
                         "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
        with TestClient(app) as client:
            preference = client.put(
                f"/profiles/fixture/company-preferences/{blacklisted['company_id']}",
                json={"status": "BLACKLISTED", "request_id": "blacklist-search"},
            )
            self.assertEqual(preference.status_code, 200, preference.text)

            filtered = client.get(
                "/jobs", params={"profile_id": "fixture", "sort": "title", "limit": 1})
            self.assertEqual(filtered.status_code, 200, filtered.text)
            self.assertEqual([row["id"] for row in filtered.json()["jobs"]], [visible["id"]])

            included = client.get("/jobs", params={
                "profile_id": "fixture", "sort": "title", "limit": 2,
                "include_blacklisted": "true",
            })
            self.assertEqual(included.status_code, 200, included.text)
            self.assertEqual({row["id"] for row in included.json()["jobs"]},
                             {blacklisted["id"], visible["id"]})

            other_profile = client.get("/jobs", params={
                "profile_id": "other", "sort": "title", "limit": 2,
            })
            self.assertEqual({row["id"] for row in other_profile.json()["jobs"]},
                             {blacklisted["id"], visible["id"]})
