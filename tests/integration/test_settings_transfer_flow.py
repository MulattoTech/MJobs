"""Connected acceptance for profile-scoped versioned settings transfer."""
import copy
import os
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_history import list_search_history
from services.discovery.sources.collect import collect_source
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


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
        self.assertEqual(exported["schema_version"], 2)
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

    def test_deleting_last_search_through_import_preserves_data_and_fences_queued_collection(self):
        enabled = self.client.put("/sources", json={
            "tenant": "settings-transfer-fixture", "enabled": True,
            "poll_interval": 600, "concurrency": 2, "request_timeout": 8,
            "rate_limit": 20, "jitter_seconds": 5,
        })
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertEqual(enabled.json()["id"], self.source_id)
        environment = {
            "MJOBS_DATABASE_URL": self.database_url, "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_OUTPUT_DIR": str(self.root), "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
        }
        captured_requests = []

        def provider(request):
            captured_requests.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.host, "boards-api.greenhouse.io")
            if request.url.path.endswith("/jobs"):
                raw = {**RAW, "title": "Systems Administrator",
                       "absolute_url": "https://job-boards.greenhouse.io/settings-transfer-fixture/jobs/1234"}
                return httpx.Response(200, json={"jobs": [raw], "meta": {"total": 1}})
            return httpx.Response(200, json={"name": "Synthetic Settings Employer"})

        # Populate actual source/job/history rows through the worker and real
        # collector. Only the provider transport is synthetic.
        source_search = self._profile_searches("fixture")[0]["id"]
        first = self.client.post(f"/sources/{self.source_id}/collect", json={
            "profile_id": "fixture", "search_profile_id": source_search,
            "request_id": "retained-history-before-delete",
        })
        self.assertEqual(first.status_code, 202, first.text)
        with patch.dict(os.environ, environment), patch(
                "workers.tasks.collect_source",
                partial(collect_source, transport=httpx.MockTransport(provider))):
            first_result = execute_operation(first.json()["operation_id"])
        self.assertEqual(first_result["status"], "COMPLETED", first_result)
        self.assertTrue(captured_requests)
        self.assertEqual(self.store.count("canonical_jobs"), 1)
        history_before = list_search_history(self.store, "fixture")
        self.assertEqual(len(history_before["runs"]), 1)
        self.assertEqual(history_before["runs"][0]["status"], "COMPLETED")

        target_search = self._profile_searches("other-profile")[0]["id"]
        queued = self.client.post(f"/sources/{self.source_id}/collect", json={
            "profile_id": "other-profile", "search_profile_id": target_search,
            "request_id": "selected-search-deleted-before-start",
        })
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        source_settings_before = self._export("fixture")
        profiles_before = {identifier: self.store.profile(identifier)
                           for identifier in ("fixture", "other-profile")}

        def inventory():
            with self.store.engine.connect() as connection:
                return {table.name: [dict(row) for row in connection.execute(
                    select(table).order_by(table.c.id)).mappings()]
                    for table in (m.job_sources, m.companies, m.jobs, m.postings,
                                  m.posting_snapshots, m.matches)}

        inventory_before = inventory()
        for table_name, rows in inventory_before.items():
            self.assertTrue(rows, f"Preservation fixture must populate {table_name}")
        empty = {"schema_version": 2, "settings": {
            "search_profiles": [], "automation_rules": [], "source_configuration": [],
        }}
        for _ in range(2):
            imported = self.client.post("/profiles/other-profile/settings/import", json=empty)
            self.assertEqual(imported.status_code, 200, imported.text)
            self.assertEqual(imported.json(), {
                "imported_count": 0, "search_profile_count": 0,
                "automation_rule_count": 0, "source_configuration_count": 0,
            })
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        self.assertEqual(self._export("other-profile"), empty)
        self.assertEqual(self._profile_searches("other-profile"), [])
        self.assertEqual(self._export("fixture"), source_settings_before)
        self.assertEqual(inventory(), inventory_before)
        for identifier, before in profiles_before.items():
            self.assertEqual(self.store.profile(identifier), before)

        with patch.dict(os.environ, environment), patch("workers.tasks.collect_source") as collect:
            result = execute_operation(operation_id)
        self.assertEqual(result["status"], "DEAD_LETTERED", result)
        collect.assert_not_called()
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one()
        self.assertEqual(operation["error"], "NoResultFound")
        self.assertEqual(operation["retry_count"], 0)
        self.assertIsNone(operation["next_attempt_at"])
        self.assertIsNone(operation["lease_expires_at"])
        self.assertEqual(inventory(), inventory_before)
        self.assertEqual(list_search_history(self.store, "fixture"), history_before)
        rejected = list_search_history(self.store, "other-profile")["runs"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["status"], "FAILED")

    def test_unknown_profile_cannot_export_or_import_settings(self):
        payload = self._export("fixture")
        self.assertEqual(self.client.get("/profiles/missing-profile/settings/export").status_code, 404)
        self.assertEqual(self.client.post(
            "/profiles/missing-profile/settings/import", json=payload).status_code, 404)

    def test_v1_migration_is_profile_isolated_atomic_and_durable(self):
        expected = self._export("fixture")
        legacy = copy.deepcopy(expected)
        legacy["schema_version"] = 1
        rule = legacy["settings"]["automation_rules"][0]
        legacy["settings"]["search_profiles"][0]["scheduler"] = {
            key: rule[key] for key in ("enabled", "interval_seconds")}
        original = copy.deepcopy(legacy)
        target_before = self._export("other-profile")
        for version in (True, 1.0, 2.0, "1", 0, 3, None):
            bad = {**copy.deepcopy(legacy), "schema_version": version}
            response = self.client.post("/profiles/other-profile/settings/import", json=bad)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self._export("other-profile"), target_before)
        conflict = copy.deepcopy(legacy)
        conflict["settings"]["automation_rules"][0]["interval_seconds"] = 1800
        response = self.client.post("/profiles/other-profile/settings/import", json=conflict)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._export("other-profile"), target_before)
        rules_only = copy.deepcopy(legacy)
        rules_only["settings"]["search_profiles"][0].pop("scheduler")
        for payload in (legacy, legacy, rules_only, {**copy.deepcopy(legacy), "settings": {
                **copy.deepcopy(legacy["settings"]), "automation_rules": []}}):
            response = self.client.post("/profiles/other-profile/settings/import", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self._export("other-profile"), expected)
        self.assertEqual(legacy, original)
        self.assertEqual(self._export("fixture"), expected)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        self.assertEqual(self._export("other-profile"), expected)
        self.assertEqual(self._export("other-profile")["schema_version"], 2)

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
