"""Portable settings use real source policies and durable discovery schedules."""
import copy
from concurrent.futures import ThreadPoolExecutor
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, select
from fastapi.testclient import TestClient

from apps.api.main import create_app

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.scheduler import schedule_due_search_profiles
from services.discovery.search_profiles import list_search_profiles, save_search_profile
from services.discovery.sources.collect import configure_source
from services.settings_transfer import export_settings, import_settings
from tests.integration.test_spine import PROFILE


class PortableSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-portable-settings-")
        self.addCleanup(self.directory.cleanup)
        self.source = self.open_store("source")
        self.target = self.open_store("target")
        for store in (self.source, self.target):
            self.initialize_store(store)
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(dict(PROFILE))
        self.source_id = configure_source(
            self.source, "portable-fixture", poll_interval=900, concurrency=2,
            request_timeout=8, rate_limit=20, jitter_seconds=0)
        save_search_profile(self.source, {
            "id": "portable-search", "profile_id": "fixture", "name": "Portable search",
            "enabled": True, "include_titles": ["Systems Administrator"],
            "source_ids": [self.source_id],
            "scheduler": {"enabled": True, "interval_seconds": 1800},
        })
        self.payload = export_settings(self.source, "fixture")
        self.payload["settings"]["automation_rules"] = [{
            "kind": "scheduled_search", "search_profile_name": "Portable search",
            "enabled": True, "interval_seconds": 1800,
        }]
        self.payload["settings"]["source_configuration"][0]["sources"] = [{
            "id": self.source_id, "provider": "greenhouse", "tenant": "portable-fixture",
            "enabled": True, "poll_interval": 900, "concurrency": 2,
            "request_timeout": 8, "rate_limit": 20, "jitter_seconds": 0,
        }]

    def open_store(self, name):
        store = Store(f"sqlite:///{(Path(self.directory.name) / (name + '.db')).as_posix()}")
        self.addCleanup(store.engine.dispose)
        return store

    def initialize_store(self, store):
        store.migrate()

    def sources(self, store):
        with store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.job_sources)).mappings()]

    def test_fresh_install_round_trip_all_categories_reopen_and_actual_enqueue(self):
        self.assertEqual(export_settings(self.source, "fixture"), self.payload)
        self.assertEqual(self.payload["settings"]["automation_rules"], [{
            "kind": "scheduled_search", "search_profile_name": "Portable search",
            "enabled": True, "interval_seconds": 1800,
        }])
        policy = self.payload["settings"]["source_configuration"][0]["sources"][0]
        self.assertEqual(policy, {
            "id": self.source_id, "provider": "greenhouse", "tenant": "portable-fixture",
            "enabled": True, "poll_interval": 900, "concurrency": 2,
            "request_timeout": 8, "rate_limit": 20, "jitter_seconds": 0,
        })
        result = import_settings(self.target, "fixture", self.payload)
        self.assertEqual(result["automation_rule_count"], 1)
        self.assertEqual(export_settings(self.target, "fixture"), self.payload)
        self.target.engine.dispose()
        self.target = self.open_store("target")
        self.assertEqual(export_settings(self.target, "fixture"), self.payload)
        at = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)
        scheduled = schedule_due_search_profiles(self.target, at)
        self.assertEqual(len(scheduled["scheduled"]), 1)
        self.assertEqual(schedule_due_search_profiles(self.target, at)["scheduled"], [])
        with self.target.engine.connect() as connection:
            operation = connection.execute(select(m.operations)).mappings().one()
        search = list_search_profiles(self.target, "fixture")[0]
        self.assertEqual(operation["kind"], "DISCOVER")
        self.assertEqual(operation["entity_id"], "fixture")
        self.assertEqual(operation["params"]["search_profile_id"], search["id"])
        self.assertEqual(operation["params"]["source_id"], self.source_id)

    def test_rules_only_and_legacy_scheduler_only_both_persist_real_schedule(self):
        rules_only = copy.deepcopy(self.payload)
        rules_only["settings"]["search_profiles"][0].pop("scheduler")
        import_settings(self.target, "fixture", rules_only)
        self.assertEqual(export_settings(self.target, "fixture"), self.payload)
        legacy = copy.deepcopy(self.payload)
        legacy["settings"]["automation_rules"] = []
        legacy["settings"]["source_configuration"][0].pop("sources")
        import_settings(self.target, "fixture", legacy)
        self.assertEqual(export_settings(self.target, "fixture"), self.payload)

    def test_invalid_rules_and_policies_reject_atomically_without_value_disclosure(self):
        before = export_settings(self.target, "fixture")
        cases = []
        for field, value in (("kind", "AUTO_SUBMIT"), ("enabled", 1),
                             ("interval_seconds", True), ("interval_seconds", 299),
                             ("search_profile_name", "missing"),
                             ("api_key", "PRIVATE_SETTINGS_SENTINEL")):
            payload = copy.deepcopy(self.payload)
            payload["settings"]["automation_rules"][0][field] = value
            cases.append(payload)
        conflict = copy.deepcopy(self.payload)
        conflict["settings"]["automation_rules"][0]["interval_seconds"] = 600
        cases.append(conflict)
        duplicate = copy.deepcopy(self.payload)
        duplicate["settings"]["automation_rules"] *= 2
        cases.append(duplicate)
        for field, value in (("concurrency", True), ("poll_interval", 299),
                             ("id", "foreign-source"), ("provider", "unsupported"),
                             ("tenant", "https://secret.invalid/PRIVATE_SETTINGS_SENTINEL"),
                             ("password", "PRIVATE_SETTINGS_SENTINEL")):
            payload = copy.deepcopy(self.payload)
            payload["settings"]["source_configuration"][0]["sources"][0][field] = value
            cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as caught:
                    import_settings(self.target, "fixture", payload)
                self.assertNotIn("PRIVATE_SETTINGS_SENTINEL", str(caught.exception))
                self.assertEqual(export_settings(self.target, "fixture"), before)
                self.assertEqual(self.sources(self.target), [])

    def test_existing_shared_source_mismatch_and_later_failure_preserve_other_profiles(self):
        other = {**dict(PROFILE), "id": "other-profile"}
        self.target.save_profile(other)
        configure_source(self.target, "portable-fixture", enabled=False)
        save_search_profile(self.target, {
            "id": "other-search", "profile_id": "other-profile", "name": "Other search",
            "source_ids": [self.source_id], "enabled": False,
        })
        before_sources = self.sources(self.target)
        before_other = export_settings(self.target, "other-profile")
        before = export_settings(self.target, "fixture")
        with self.assertRaisesRegex(ValueError, "policy"):
            import_settings(self.target, "fixture", self.payload)
        self.assertEqual(self.sources(self.target), before_sources)
        self.assertEqual(export_settings(self.target, "other-profile"), before_other)
        self.assertEqual(export_settings(self.target, "fixture"), before)

    def test_failure_after_new_source_admission_rolls_back_every_category(self):
        before = export_settings(self.target, "fixture")
        # A legacy reference without a policy is discovered only after the
        # new portable source has been admitted inside the transaction.
        payload = copy.deepcopy(self.payload)
        payload["settings"]["search_profiles"].append({
            "name": "Legacy missing source", "enabled": False, "criteria": {}})
        payload["settings"]["source_configuration"].append({
            "search_profile_name": "Legacy missing source", "source_ids": ["missing-source"]})
        with self.assertRaisesRegex(ValueError, "unavailable source"):
            import_settings(self.target, "fixture", payload)
        self.assertEqual(export_settings(self.target, "fixture"), before)
        self.assertEqual(self.sources(self.target), [])
        with patch.object(self.target, "_audit", side_effect=RuntimeError("synthetic interruption")):
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                import_settings(self.target, "fixture", self.payload)
        self.assertEqual(export_settings(self.target, "fixture"), before)
        self.assertEqual(self.sources(self.target), [])
        import_settings(self.target, "fixture", self.payload)
        self.assertEqual(export_settings(self.target, "fixture"), self.payload)

    def test_unknown_profile_and_secret_stored_source_cannot_create_or_export_policy(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            import_settings(self.target, "unknown-profile", self.payload)
        self.assertEqual(self.sources(self.target), [])
        with self.source.engine.begin() as connection:
            connection.execute(m.job_sources.update().where(m.job_sources.c.id == self.source_id).values(
                tenant="https://secret.invalid/PRIVATE_SETTINGS_SENTINEL"))
        with self.assertRaises(ValueError) as caught:
            export_settings(self.source, "fixture")
        self.assertNotIn("PRIVATE_SETTINGS_SENTINEL", str(caught.exception))

    def test_api_import_fresh_sources_counts_rejection_and_other_profile_isolation(self):
        self.target.save_profile({**dict(PROFILE), "id": "other-profile"})
        save_search_profile(self.target, {
            "id": "other-search", "profile_id": "other-profile", "name": "Other",
            "enabled": False, "source_ids": [],
        })
        other_before = export_settings(self.target, "other-profile")
        app = create_app(self.target, "http://127.0.0.1:1", Path(self.directory.name),
                         "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
        with TestClient(app, raise_server_exceptions=False) as client:
            rejected = copy.deepcopy(self.payload)
            rejected["settings"]["source_configuration"][0]["sources"][0]["api_key"] = (
                "PRIVATE_SETTINGS_SENTINEL")
            response = client.post("/profiles/fixture/settings/import", json=rejected)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn("PRIVATE_SETTINGS_SENTINEL", response.text)
            self.assertEqual(self.sources(self.target), [])
            response = client.post("/profiles/fixture/settings/import", json=self.payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["automation_rule_count"], 1)
            self.assertEqual(client.get("/profiles/fixture/settings/export").json(), self.payload)
            self.assertEqual(client.get("/profiles/other-profile/settings/export").json(), other_before)
        self.assertEqual(export_settings(self.source, "fixture"), self.payload)

    def test_api_bounds_actual_stream_before_parsing_and_preserves_settings(self):
        before = export_settings(self.target, "fixture")
        app = create_app(self.target, "http://127.0.0.1:1", Path(self.directory.name),
                         "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/profiles/fixture/settings/import", content=b" " * (1024 * 1024 + 1),
                                   headers={"content-type": "application/json", "content-length": "1"})
            self.assertEqual(response.status_code, 413, response.text)
            response = client.post("/profiles/fixture/settings/import",
                                   content=iter([b" " * (512 * 1024), b" " * (512 * 1024 + 1)]),
                                   headers={"content-type": "application/json"})
            self.assertEqual(response.status_code, 413, response.text)
            self.assertEqual(export_settings(self.target, "fixture"), before)
            self.assertEqual(self.sources(self.target), [])

    def test_scheduler_tick_after_replacement_never_enqueues_removed_search(self):
        configure_source(self.target, "portable-fixture", poll_interval=900, concurrency=2,
                         request_timeout=8, rate_limit=20, jitter_seconds=0)
        save_search_profile(self.target, {
            "id": "old-search", "profile_id": "fixture", "name": "Old search", "enabled": True,
            "source_ids": [self.source_id], "scheduler": {"enabled": True, "interval_seconds": 600}})
        payload = copy.deepcopy(self.payload)
        payload["settings"]["search_profiles"][0]["enabled"] = False
        imported = threading.Event()
        release = threading.Event()
        tick_read = threading.Event()
        tick_store = self.open_store("target")
        original_audit = self.target._audit

        def hold_import(connection, entity, action, data):
            original_audit(connection, entity, action, data)
            if action == "SETTINGS_IMPORTED":
                imported.set()
                if not release.wait(5):
                    raise RuntimeError("Synthetic import fence timed out")

        def observed_read(connection, cursor, statement, parameters, context, executemany):
            if "search_profiles" in statement and statement.lstrip().upper().startswith("SELECT"):
                tick_read.set()
        event.listen(tick_store.engine, "after_cursor_execute", observed_read)
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.target, "_audit", hold_import):
            writer = pool.submit(import_settings, self.target, "fixture", payload)
            try:
                self.assertTrue(imported.wait(5))
                tick = pool.submit(schedule_due_search_profiles, tick_store)
                # Before the fence, the tick reads the old set here. With the
                # fence it waits for replacement, then sees the disabled set.
                tick_read.wait(.4)
            finally:
                release.set()
            writer.result(timeout=5)
            self.assertEqual(tick.result(timeout=5)["scheduled"], [])
        event.remove(tick_store.engine, "after_cursor_execute", observed_read)

    def test_partial_saved_search_edit_preserves_replacement_criteria(self):
        save_search_profile(self.target, {
            "id": "existing", "profile_id": "fixture", "name": "Existing", "optional_keywords": ["old"]})
        edit_store = self.open_store("target")
        attempted_read = threading.Event()

        def observed_read(connection, cursor, statement, parameters, context, executemany):
            if "search_profiles" in statement and statement.lstrip().upper().startswith("SELECT"):
                attempted_read.set()
        event.listen(edit_store.engine, "after_cursor_execute", observed_read)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.target.engine.begin() as connection:
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                connection.execute(select(m.profiles.c.id).where(
                    m.profiles.c.id == "fixture").with_for_update()).one()
                row = connection.execute(select(m.search_profiles).where(
                    m.search_profiles.c.id == "existing")).mappings().one()
                criteria = {**row["criteria"], "optional_keywords": ["replacement"]}
                connection.execute(m.search_profiles.update().where(
                    m.search_profiles.c.id == "existing").values(criteria=criteria))
                edited = pool.submit(save_search_profile, edit_store, {
                    "id": "existing", "profile_id": "fixture", "name": "Existing",
                    "scheduler": {"enabled": False, "interval_seconds": 900}})
                attempted_read.wait(.4)
            edited.result(timeout=5)
        event.remove(edit_store.engine, "after_cursor_execute", observed_read)
        self.assertEqual(list_search_profiles(self.target, "fixture")[0]["criteria"]["optional_keywords"],
                         ["replacement"])


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Requires an explicitly configured disposable PostgreSQL test database")
class PostgreSQLPortableSettingsTests(PortableSettingsTests):
    """The same persistence/recovery contracts run in unique disposable schemas."""
    def setUp(self):
        self.schemas = {}
        super().setUp()

    def open_store(self, name):
        store = Store(os.environ["MJOBS_TEST_POSTGRES_URL"])
        if name not in self.schemas:
            schema = "mjobs_settings_" + uuid4().hex
            self.schemas[name] = schema
            with store.engine.begin() as connection:
                connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

            def remove_schema():
                cleanup = Store(os.environ["MJOBS_TEST_POSTGRES_URL"])
                try:
                    with cleanup.engine.begin() as connection:
                        connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
                finally:
                    cleanup.engine.dispose()
            self.addCleanup(remove_schema)
        store.engine = store.engine.execution_options(schema_translate_map={None: self.schemas[name]})
        self.addCleanup(store.engine.dispose)
        return store

    def initialize_store(self, store):
        m.metadata.create_all(store.engine)


if __name__ == "__main__":
    unittest.main()
