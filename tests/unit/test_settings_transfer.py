"""Profile-scoped settings transfer validates before replacing durable settings."""
from pathlib import Path
import copy
import tempfile
import unittest

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import list_search_profiles, save_search_profile
from services.discovery.sources.collect import configure_source
from services.settings_transfer import export_settings, import_settings
from tests.integration.test_spine import PROFILE


class SettingsTransferTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-settings-transfer-")
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'settings.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(dict(PROFILE))
        self.source_id = configure_source(self.store, "settings-transfer-test", enabled=True)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def add_search(self, *, profile_id="fixture", search_id="search-original", name="Original"):
        save_search_profile(self.store, {
            "id": search_id,
            "profile_id": profile_id,
            "name": name,
            "enabled": False,
            "include_titles": ["Systems Administrator"],
            "optional_keywords": ["Windows Server"],
            "source_ids": [self.source_id],
            "scheduler": {"enabled": True, "interval_seconds": 1800},
        })

    def test_export_has_versioned_allowlisted_profile_settings_and_no_owner_or_secrets(self):
        self.add_search()

        exported = export_settings(self.store, "fixture")

        self.assertEqual(set(exported), {"schema_version", "settings"})
        self.assertEqual(exported["schema_version"], 2)
        self.assertNotIn("scheduler", exported["settings"]["search_profiles"][0])
        self.assertEqual(set(exported["settings"]), {
            "search_profiles", "automation_rules", "source_configuration",
        })
        self.assertEqual(exported["settings"]["automation_rules"], [{
            "kind": "scheduled_search", "search_profile_name": "Original",
            "enabled": True, "interval_seconds": 1800,
        }])
        profile = exported["settings"]["search_profiles"][0]
        self.assertEqual(profile["name"], "Original")
        self.assertNotIn("profile_id", profile)
        self.assertNotIn("id", profile)
        self.assertEqual(exported["settings"]["source_configuration"], [{
            "search_profile_name": "Original", "source_ids": [self.source_id],
            "sources": [{"id": self.source_id, "provider": "greenhouse", "tenant": "settings-transfer-test",
                         "enabled": True, "poll_interval": 300, "concurrency": 1,
                         "request_timeout": 10, "rate_limit": 60, "jitter_seconds": 30}],
        }])
        self.assertNotIn("credentials", repr(exported).lower())
        self.assertNotIn("password", repr(exported).lower())

    def test_import_replaces_only_target_profile_settings_and_survives_store_reopen(self):
        self.add_search()
        other_profile = dict(PROFILE)
        other_profile["id"] = "other-profile"
        self.store.save_profile(other_profile)
        self.add_search(profile_id="other-profile", search_id="other-search", name="Other")
        payload = export_settings(self.store, "fixture")
        payload["settings"]["search_profiles"][0]["name"] = "Imported"
        payload["settings"]["source_configuration"][0]["search_profile_name"] = "Imported"
        payload["settings"]["automation_rules"][0]["search_profile_name"] = "Imported"

        result = import_settings(self.store, "fixture", payload)

        self.assertEqual(result["imported_count"], 1)
        self.assertEqual([row["name"] for row in list_search_profiles(self.store, "fixture")], ["Imported"])
        self.assertEqual([row["name"] for row in list_search_profiles(self.store, "other-profile")], ["Other"])
        self.assertEqual(list_search_profiles(self.store, "fixture")[0]["source_ids"], [self.source_id])
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'settings.db').as_posix()}")
        self.assertEqual([row["name"] for row in list_search_profiles(self.store, "fixture")], ["Imported"])

    def test_invalid_payload_never_partially_replaces_existing_settings(self):
        self.add_search()
        payload = export_settings(self.store, "fixture")
        payload["settings"]["search_profiles"][0]["name"] = "Would be partial"
        payload["settings"]["search_profiles"].append({
            "name": "Invalid", "enabled": True, "criteria": {"required_titles": ["bad"]},
        })
        before = copy.deepcopy(list_search_profiles(self.store, "fixture"))

        with self.assertRaises(ValueError):
            import_settings(self.store, "fixture", payload)

        self.assertEqual(list_search_profiles(self.store, "fixture"), before)

    def test_unsupported_version_secret_fields_and_unmodeled_automation_rules_are_rejected(self):
        self.add_search()
        original = export_settings(self.store, "fixture")
        cases = []
        wrong_version = copy.deepcopy(original)
        wrong_version["schema_version"] = 3
        cases.append(wrong_version)
        secret = copy.deepcopy(original)
        secret["settings"]["search_profiles"][0]["api_token"] = "must-not-import"
        cases.append(secret)
        automation = copy.deepcopy(original)
        automation["settings"]["automation_rules"] = [{"mode": "AUTO_SUBMIT"}]
        cases.append(automation)

        for payload in cases:
            with self.subTest(payload=payload):
                before = copy.deepcopy(list_search_profiles(self.store, "fixture"))
                with self.assertRaises(ValueError):
                    import_settings(self.store, "fixture", payload)
                self.assertEqual(list_search_profiles(self.store, "fixture"), before)

    def test_missing_or_foreign_source_reference_rejects_without_global_source_mutation(self):
        self.add_search()
        payload = export_settings(self.store, "fixture")
        payload["settings"]["source_configuration"][0]["source_ids"] = ["missing-source"]
        before = copy.deepcopy(list_search_profiles(self.store, "fixture"))
        with self.store.engine.connect() as connection:
            source_enabled_before = connection.execute(
                m.job_sources.select().with_only_columns(m.job_sources.c.enabled)
            ).scalar_one()

        with self.assertRaises(ValueError):
            import_settings(self.store, "fixture", payload)

        self.assertEqual(list_search_profiles(self.store, "fixture"), before)
        with self.store.engine.connect() as connection:
            source_enabled_after = connection.execute(
                m.job_sources.select().with_only_columns(m.job_sources.c.enabled)
            ).scalar_one()
        self.assertEqual(source_enabled_after, source_enabled_before)


    def test_real_v1_scheduler_only_migrates_without_mutating_input(self):
        self.add_search()
        expected = export_settings(self.store, "fixture")
        legacy = copy.deepcopy(expected)
        legacy["schema_version"] = 1
        rule = legacy["settings"]["automation_rules"].pop()
        legacy["settings"]["search_profiles"][0]["scheduler"] = {
            key: rule[key] for key in ("enabled", "interval_seconds")}
        original = copy.deepcopy(legacy)
        import_settings(self.store, "fixture", legacy)
        self.assertEqual(legacy, original)
        self.assertEqual(export_settings(self.store, "fixture"), expected)
        self.assertEqual(export_settings(self.store, "fixture")["schema_version"], 2)

    def test_v2_inline_scheduler_and_invalid_versions_reject_without_changes(self):
        self.add_search()
        before = export_settings(self.store, "fixture")
        payload = copy.deepcopy(before)
        payload["schema_version"] = 2
        payload["settings"]["search_profiles"][0]["scheduler"] = {
            "enabled": True, "interval_seconds": 1800}
        cases = [payload] + [{**copy.deepcopy(before), "schema_version": version}
                             for version in (True, False, 1.0, 2.0, "1", "2", 0, -1, 3, None)]
        for payload in cases:
            with self.subTest(version=payload["schema_version"]):
                with self.assertRaises(ValueError):
                    import_settings(self.store, "fixture", payload)
                self.assertEqual(export_settings(self.store, "fixture"), before)


if __name__ == "__main__":
    unittest.main()
