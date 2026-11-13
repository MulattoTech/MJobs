"""Restored logical task settings stay editable without replacing imported IDs."""

import copy
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.ai.tasks import AI_TASKS
from services.data_export import export_profile_backup
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url
from tests.integration.test_spine import PROFILE


def _selections(label):
    return {task: {"provider": "ollama", "model": f"synthetic-{label}-{task}"}
            for task in AI_TASKS}


class _TaskSettingsIdentityContract:
    def prepare_fixture(self):
        self.store.bind_workspace_mode("FIXTURE")
        for profile_id in ("source", "other"):
            profile = copy.deepcopy(PROFILE)
            profile.update(id=profile_id, name=f"Synthetic {profile_id}",
                           email=f"{profile_id}@example.invalid")
            self.store.save_profile(profile)
        self.enterContext(patch.dict(os.environ, {
            "MJOBS_OLLAMA_BASE_URL": "http://127.0.0.1:11434",
        }))
        for target in (
            "services.ai.providers.ollama.OllamaProvider.generate",
            "httpx.HTTPTransport.handle_request",
            "httpx.AsyncHTTPTransport.handle_async_request",
        ):
            guard = self.enterContext(patch(
                target, side_effect=AssertionError("settings must not invoke providers")))
            self.addCleanup(guard.assert_not_called)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        self.addCleanup(self.client.close)

    def save_api(self, profile_id, selections):
        result = self.client.put(f"/profiles/{profile_id}/ai-task-settings",
                                 json={"tasks": selections})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["profile_id"], profile_id)
        self.assertEqual(result.json()["tasks"], selections)
        return result.json()["tasks"]

    def rows(self, profile_id):
        with self.store.engine.connect() as connection:
            return {row["task"]: dict(row) for row in connection.execute(
                select(m.ai_task_settings).where(
                    m.ai_task_settings.c.profile_id == profile_id,
                )).mappings()}

    def setting_events(self, profile_id):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(m.audit.c.id, m.audit.c.payload).where(
                    m.audit.c.entity_id == profile_id,
                    m.audit.c.action == "SETTING_CHANGED",
                ).order_by(m.audit.c.id)).mappings()]

    def restored_profile(self):
        self.store.save_ai_task_settings("source", _selections("source"))
        self.store.save_ai_task_settings("other", _selections("other"))
        backup = export_profile_backup(self.store, "source", output_dir=self.root)
        preview = self.client.post("/profile-restores/preview", json={"backup": backup})
        self.assertEqual(preview.status_code, 200, preview.text)
        result = self.client.post("/profile-restores", json={
            "backup": backup, "request_id": "restored-settings-" + uuid4().hex,
            "expected_fingerprint": preview.json()["fingerprint"], "confirmed": True,
        })
        self.assertEqual(result.status_code, 201, result.text)
        profile_id = result.json()["profile_id"]
        self.assertNotIn(profile_id, ("source", "other"))
        restored = self.rows(profile_id)
        self.assertEqual(set(restored), set(AI_TASKS))
        self.assertTrue(all(row["id"] != identity(profile_id, task)
                            for task, row in restored.items()))
        self.assertEqual(self.store.ai_task_settings(profile_id), {
            task: {"provider": "disabled", "model": None} for task in AI_TASKS
        })
        return profile_id

    def test_restored_settings_keep_ids_noop_audit_and_durable_values(self):
        restored_id = self.restored_profile()
        source_before, other_before = self.rows("source"), self.rows("other")
        imported_ids = {task: row["id"] for task, row in self.rows(restored_id).items()}
        disabled = {task: {"provider": "disabled", "model": None} for task in AI_TASKS}
        self.assertEqual(self.setting_events(restored_id), [])

        # An ordinary Save of disabled imports used to violate the natural key.
        self.assertEqual(self.save_api(restored_id, disabled), disabled)
        self.assertEqual(self.setting_events(restored_id), [])
        desired = _selections("restored")
        self.assertEqual(self.save_api(restored_id, desired), desired)
        events = self.setting_events(restored_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"], {})
        self.assertEqual(self.save_api(restored_id, desired), desired)
        self.assertEqual(self.setting_events(restored_id), events)
        self.assertEqual(
            {task: row["id"] for task, row in self.rows(restored_id).items()},
            imported_ids,
        )
        self.assertEqual(self.rows("source"), source_before)
        self.assertEqual(self.rows("other"), other_before)

        reopened = Store(self.database_url)
        try:
            self.assertEqual(reopened.ai_task_settings(restored_id), desired)
            with reopened.engine.connect() as connection:
                saved = {row["task"]: row["id"] for row in connection.execute(
                    select(m.ai_task_settings).where(
                        m.ai_task_settings.c.profile_id == restored_id,
                    )).mappings()}
            self.assertEqual(saved, imported_ids)
        finally:
            reopened.engine.dispose()

    def test_failed_later_selection_rolls_back_rows_timestamps_and_audit(self):
        restored_id = self.restored_profile()
        before = self.rows(restored_id)
        events = self.setting_events(restored_id)
        other_before = self.rows("other")
        invalid_api = _selections("rejected")
        invalid_api["company_summarization"]["provider"] = "synthetic-private-provider"
        rejected = self.client.put(f"/profiles/{restored_id}/ai-task-settings",
                                   json={"tasks": invalid_api})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(rejected.json()["detail"]["code"], "AI_PROVIDER_UNSUPPORTED")
        self.assertNotIn("synthetic-private-provider", rejected.text)
        self.assertEqual(self.rows(restored_id), before)
        self.assertEqual(self.setting_events(restored_id), events)
        invalid_later = (
            ({"provider": "disabled", "model": None, "extra": True}, ValueError),
            ({"provider": "disabled", "model": "invalid-disabled-model"}, IntegrityError),
        )
        for invalid, expected in invalid_later:
            with self.subTest(error=expected.__name__), self.assertRaises(expected):
                self.store.save_ai_task_settings(restored_id, {
                    "job_parsing": {"provider": "ollama", "model": "synthetic-rollback"},
                    "job_scoring": invalid,
                })
            self.assertEqual(self.rows(restored_id), before)
            self.assertEqual(self.setting_events(restored_id), events)
            self.assertEqual(self.rows("other"), other_before)

    def test_partial_save_preserves_unselected_tasks_and_other_profiles(self):
        restored_id = self.restored_profile()
        before, other_before = self.rows(restored_id), self.rows("other")
        changed = {"provider": "ollama", "model": "synthetic-one-task"}
        actual = self.store.save_ai_task_settings(restored_id, {"job_scoring": changed})
        self.assertEqual(actual["job_scoring"], changed)
        after = self.rows(restored_id)
        self.assertEqual(after["job_scoring"]["id"], before["job_scoring"]["id"])
        self.assertEqual(
            {task: row for task, row in after.items() if task != "job_scoring"},
            {task: row for task, row in before.items() if task != "job_scoring"},
        )
        self.assertEqual(self.rows("other"), other_before)
        self.assertEqual(len(self.setting_events(restored_id)), 1)

    def test_concurrent_first_saves_and_restored_updates_remain_atomic(self):
        restored_id = self.restored_profile()
        imported_ids = {task: row["id"] for task, row in self.rows(restored_id).items()}
        fresh = copy.deepcopy(PROFILE)
        fresh["id"] = "fresh"
        self.store.save_profile(fresh)
        for profile_id in ("fresh", restored_id):
            barrier = threading.Barrier(2)

            def save(label, selected_profile, start_barrier):
                owned = Store(self.database_url)
                try:
                    start_barrier.wait(timeout=10)
                    owned.save_ai_task_settings(selected_profile, _selections(label))
                finally:
                    owned.engine.dispose()

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(save, label, profile_id, barrier)
                           for label in ("one", "two")]
                for future in futures:
                    future.result(timeout=30)
            with self.subTest(profile_id=profile_id):
                actual = self.store.ai_task_settings(profile_id)
                self.assertIn(actual, (_selections("one"), _selections("two")))
                self.assertEqual(set(self.rows(profile_id)), set(AI_TASKS))
        self.assertEqual(
            {task: row["id"] for task, row in self.rows(restored_id).items()},
            imported_ids,
        )
        self.assertTrue(all(row["id"] == identity("fresh", task)
                            for task, row in self.rows("fresh").items()))


class RestoredAITaskSettingsSQLiteTests(_TaskSettingsIdentityContract, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-ai-settings-identity-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database_url = f"sqlite:///{(self.root / 'settings.db').as_posix()}"
        self.store = Store(self.database_url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.prepare_fixture()


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Requires disposable loopback MJOBS_TEST_POSTGRES_URL")
class RestoredAITaskSettingsPostgreSQLTests(_TaskSettingsIdentityContract, unittest.TestCase):
    def setUp(self):
        url = _disposable_postgres_url(os.environ["MJOBS_TEST_POSTGRES_URL"])
        directory = tempfile.TemporaryDirectory(prefix="mjobs-ai-settings-postgres-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.schema = "mjobs_ai_settings_" + uuid4().hex
        self.admin = create_engine(url, hide_parameters=True)
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.drop_schema)
        self.database_url = url.update_query_dict({
            "options": "-csearch_path=" + self.schema
            + " -clock_timeout=5000 -cstatement_timeout=30000",
        }).render_as_string(hide_password=False)
        self.store = Store(self.database_url)
        self.addCleanup(self.store.engine.dispose)
        # Private schema tests exercise current relational constraints, not a
        # production migration or an arbitrary existing database.
        from packages.persistence import resume_workspace  # noqa: F401
        m.metadata.create_all(self.store.engine)
        self.prepare_fixture()

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))


if __name__ == "__main__":
    unittest.main()
