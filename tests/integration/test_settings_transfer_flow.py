"""Connected acceptance for profile-scoped versioned settings transfer."""
import copy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class SettingsTransferFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-settings-transfer-")
        self.root = Path(self.directory.name)
        self.database_url = f"sqlite:///{(self.root / 'settings.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        source_profile = copy.deepcopy(PROFILE)
        source_profile["facts"].append({
            "id": "private-fact",
            "kind": "experience",
            "text": "PRIVATE_CAREER_FACT_SENTINEL",
            "source": "synthetic-test-only",
            "confidence": 100,
            "verified": False,
            "user_approved": False,
        })
        self.store.save_profile(source_profile)
        other = copy.deepcopy(PROFILE)
        other.update(id="other-profile", name="Other Fixture", email="other@example.test")
        self.store.save_profile(other)
        self.client = self._client()
        source = self.client.put("/sources", json={
            "tenant": "settings-transfer-fixture",
            "enabled": False,
            "poll_interval": 600,
            "concurrency": 2,
            "request_timeout": 8,
            "rate_limit": 20,
            "jitter_seconds": 5,
        })
        self.assertEqual(source.status_code, 200, source.text)
        self.source_id = source.json()["id"]
        self._save_search("fixture", "Infrastructure search", ["Systems Administrator"])
        self._save_search("other-profile", "Local other search", ["Network Engineer"], source_ids=[])

    def _client(self):
        return TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"), raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _save_search(self, profile_id, name, titles, source_ids=None):
        response = self.client.put("/search-profiles", json={
            "id": f"{profile_id}-{name.lower().replace(' ', '-')}",
            "profile_id": profile_id,
            "name": name,
            "enabled": True,
            "include_titles": titles,
            "remote": "REQUIRED",
            "source_ids": [self.source_id] if source_ids is None else source_ids,
            "scheduler": {"enabled": False, "interval_seconds": 600},
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _export(self, profile_id):
        response = self.client.get(f"/profiles/{profile_id}/settings/export")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _profile_searches(self, profile_id):
        response = self.client.get("/search-profiles", params={"profile_id": profile_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["search_profiles"]

    def test_versioned_export_import_is_profile_scoped_and_survives_reload(self):
        exported = self._export("fixture")
        self.assertEqual(set(exported), {"schema_version", "settings"})
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(set(exported["settings"]), {
            "search_profiles", "automation_rules", "source_configuration",
        })
        self.assertEqual(len(exported["settings"]["search_profiles"]), 1)
        saved_search = exported["settings"]["search_profiles"][0]
        self.assertEqual(saved_search["name"], "Infrastructure search")
        self.assertEqual(saved_search["criteria"]["include_titles"], ["Systems Administrator"])
        self.assertNotIn("id", saved_search)
        self.assertNotIn("profile_id", saved_search)
        self.assertEqual(exported["settings"]["automation_rules"], [{
            "kind": "scheduled_search", "search_profile_name": "Infrastructure search",
            "enabled": False, "interval_seconds": 600,
        }])
        self.assertEqual(exported["settings"]["source_configuration"], [{
            "search_profile_name": "Infrastructure search",
            "source_ids": [self.source_id],
            "sources": [{"id": self.source_id, "provider": "greenhouse", "tenant": "settings-transfer-fixture",
                         "enabled": False, "poll_interval": 600, "concurrency": 2,
                         "request_timeout": 8, "rate_limit": 20, "jitter_seconds": 5}],
        }])
        for private_value in (
                "Alex Fixture", "alex@example.test", "PRIVATE_CAREER_FACT_SENTINEL",
                "fictional-test-profile", "resume", "document"):
            self.assertNotIn(private_value.casefold(), str(exported).casefold())

        imported = self.client.post("/profiles/other-profile/settings/import", json=exported)
        self.assertEqual(imported.status_code, 200, imported.text)
        imported_export = self._export("other-profile")
        self.assertEqual(imported_export["settings"], exported["settings"])
        self.assertEqual([row["name"] for row in self._profile_searches("other-profile")],
                         ["Infrastructure search"])
        self.assertEqual([row["name"] for row in self._profile_searches("fixture")],
                         ["Infrastructure search"])

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        self.assertEqual(self._export("other-profile"), imported_export)

    def test_invalid_or_unsupported_imports_leave_all_profile_settings_unchanged(self):
        before = self._export("other-profile")
        valid_search = copy.deepcopy(self._export("fixture")["settings"]["search_profiles"][0])
        changed_search = {**valid_search, "name": "Must not partially import"}
        malformed = copy.deepcopy(before)
        malformed["settings"]["search_profiles"] = [changed_search, {"unexpected": "field"}]
        secret_bearing = copy.deepcopy(before)
        secret_bearing["settings"]["search_profiles"] = [{**changed_search, "api_key": "synthetic-private-token"}]
        career_data = copy.deepcopy(before)
        career_data["settings"]["career_facts"] = [{"text": "PRIVATE_CAREER_FACT_SENTINEL"}]
        unsupported_rules = copy.deepcopy(before)
        unsupported_rules["settings"]["automation_rules"] = [{"name": "unsupported rule"}]
        unsupported_version = {**copy.deepcopy(before), "schema_version": 999}

        for case, payload in (
                ("malformed later record", malformed),
                ("secret-bearing payload", secret_bearing),
                ("career facts are not transferable", career_data),
                ("automation rules without a supported model", unsupported_rules),
                ("unsupported schema version", unsupported_version)):
            with self.subTest(case=case):
                response = self.client.post("/profiles/other-profile/settings/import", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                if case == "secret-bearing payload":
                    self.assertNotIn("synthetic-private-token", response.text)
                self.assertEqual(self._export("other-profile"), before)

    def test_unknown_profile_cannot_export_or_import_settings(self):
        payload = self._export("fixture")
        self.assertEqual(self.client.get("/profiles/missing-profile/settings/export").status_code, 404)
        self.assertEqual(self.client.post(
            "/profiles/missing-profile/settings/import", json=payload).status_code, 404)

    def test_malformed_criteria_rejects_without_replacement_and_recovers_after_reopen(self):
        before = self._export("other-profile")
        source_before = self._export("fixture")
        for field in ("remote", "salary_period", "date_window"):
            for value in ([], {"private": "PRIVATE_IMPORT_SENTINEL"}):
                with self.subTest(field=field, value=value):
                    payload = copy.deepcopy(source_before)
                    payload["settings"]["search_profiles"][0]["criteria"][field] = value
                    response = self.client.post("/profiles/other-profile/settings/import", json=payload)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertNotIn("PRIVATE_IMPORT_SENTINEL", response.text)
                    self.assertEqual(self._export("other-profile"), before)
                    self.assertEqual(self._export("fixture"), source_before)

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        self.assertEqual(self._export("other-profile"), before)
        response = self.client.post("/profiles/other-profile/settings/import", json=source_before)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._export("other-profile"), source_before)


if __name__ == "__main__":
    unittest.main()
