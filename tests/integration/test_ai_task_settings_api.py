"""Profile-scoped AI task settings persist without storing credentials."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.ai.tasks import AI_TASKS
from tests.integration.test_spine import PROFILE


class AITaskSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database_url = f"sqlite:///{(self.root / 'settings.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(copy.deepcopy(PROFILE))
        second = copy.deepcopy(PROFILE)
        second.update(id="other-profile", name="Other Profile", email="other@example.test")
        self.store.save_profile(second)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root,
                                            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_settings_are_profile_scoped_and_survive_a_store_reopen(self):
        initial = self.client.get("/profiles/fixture/ai-task-settings")
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(set(initial.json()["tasks"]), set(AI_TASKS))
        self.assertTrue(all(value == {"provider": "disabled", "model": None}
                            for value in initial.json()["tasks"].values()))
        self.assertEqual(set(initial.json()["fallback_capabilities"]), set(AI_TASKS))

        configured = {task: {"provider": "disabled", "model": None} for task in AI_TASKS}
        configured["job_scoring"] = {"provider": "ollama", "model": "llama3.2:3b"}
        saved = self.client.put("/profiles/fixture/ai-task-settings",
                                json={"tasks": configured})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["tasks"], configured)

        other = self.client.get("/profiles/other-profile/ai-task-settings")
        self.assertEqual(other.status_code, 200, other.text)
        self.assertTrue(all(value["provider"] == "disabled"
                            for value in other.json()["tasks"].values()))

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root,
                                            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))
        reloaded = self.client.get("/profiles/fixture/ai-task-settings")
        self.assertEqual(reloaded.status_code, 200, reloaded.text)
        self.assertEqual(reloaded.json()["tasks"], configured)

    def test_ai_task_setting_changes_append_one_value_free_profile_event(self):
        defaults = {task: {"provider": "disabled", "model": None} for task in AI_TASKS}

        initial_save = self.client.put("/profiles/fixture/ai-task-settings",
                                       json={"tasks": defaults})
        self.assertEqual(initial_save.status_code, 200, initial_save.text)
        before_change = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(before_change.status_code, 200, before_change.text)
        self.assertFalse(any(event["type"] == "SETTING_CHANGED"
                             for event in before_change.json()["events"]))

        configured = {**defaults, "job_scoring": {"provider": "ollama",
                                                    "model": "llama3.2:3b"}}
        changed = self.client.put("/profiles/fixture/ai-task-settings",
                                  json={"tasks": configured})
        self.assertEqual(changed.status_code, 200, changed.text)
        unchanged = self.client.put("/profiles/fixture/ai-task-settings",
                                    json={"tasks": configured})
        self.assertEqual(unchanged.status_code, 200, unchanged.text)

        history = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(history.status_code, 200, history.text)
        setting_events = [event for event in history.json()["events"]
                          if event["type"] == "SETTING_CHANGED"]
        self.assertEqual(len(setting_events), 1)
        self.assertEqual(setting_events[0]["summary"], "A profile setting changed")
        self.assertNotIn("job_scoring", history.text)
        self.assertNotIn("ollama", history.text)
        self.assertNotIn("llama3.2:3b", history.text)
        with self.store.engine.connect() as connection:
            payload = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.id == setting_events[0]["id"])).scalar_one()
        self.assertEqual(payload, {})

        other_history = self.client.get("/profiles/other-profile/audit-history")
        self.assertEqual(other_history.status_code, 200, other_history.text)
        self.assertFalse(any(event["type"] == "SETTING_CHANGED"
                             for event in other_history.json()["events"]))

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root,
                                            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))
        reloaded = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(reloaded.status_code, 200, reloaded.text)
        self.assertEqual(sum(event["type"] == "SETTING_CHANGED"
                             for event in reloaded.json()["events"]), 1)

    def test_all_disabled_keeps_deterministic_matching_and_factual_documents_available(self):
        settings = self.client.get("/profiles/fixture/ai-task-settings").json()
        self.assertTrue(all(task["provider"] == "disabled" for task in settings["tasks"].values()))
        self.assertEqual(settings["fallback_capabilities"]["job_scoring"]["mode"], "DETERMINISTIC")
        self.assertEqual(settings["fallback_capabilities"]["resume_tailoring"]["mode"], "DETERMINISTIC")
        self.assertEqual(settings["fallback_capabilities"]["cover_letters"]["mode"], "DETERMINISTIC")

        job = capture_manual_job(self.store, {
            "request_id": "no-ai-fallback-job",
            "title": "Systems Administrator",
            "company": "Synthetic Company",
            "description": "Responsibilities:\nMaintain Windows Server infrastructure, build Python automation, and support network operations with PowerShell.",
            "reference_url": "https://jobs.example.test/no-ai-fallback",
            "application_url": "https://apply.example.test/no-ai-fallback",
            "source_details": {},
        })
        recalculate(self.store, "fixture")
        with patch("services.ai.tasks.create_provider",
                   side_effect=AssertionError("disabled core fallback must not call a provider")):
            rows = self.client.get("/jobs", params={"profile_id": "fixture"}).json()["jobs"]
            scored = next(row for row in rows if row["id"] == job["job"]["id"])
            self.assertIsInstance(scored["match_score"], (int, float))
            application_id = prepare(self.store, job["job"]["id"], "fixture", self.root)

        documents = application_documents(self.store, application_id)
        self.assertEqual({row["kind"] for row in documents}, {"resume", "cover-letter"})
        resume = next(row for row in documents if row["kind"] == "resume")
        self.assertTrue(resume["fact_ids"])
        self.assertEqual(resume["profile_version"], self.store.profile("fixture")["version"])

    def test_unknown_tasks_and_unsupported_providers_fail_closed(self):
        unknown_task = self.client.put("/profiles/fixture/ai-task-settings", json={
            "tasks": {"private-task": {"provider": "disabled", "model": None}},
        })
        self.assertEqual(unknown_task.status_code, 422)
        self.assertEqual(unknown_task.json()["detail"]["code"], "AI_TASK_UNSUPPORTED")
        self.assertNotIn("private-task", unknown_task.text)

        unsupported = self.client.put("/profiles/fixture/ai-task-settings", json={
            "tasks": {"job_scoring": {"provider": "unsupported-provider", "model": "synthetic-model"}},
        })
        self.assertEqual(unsupported.status_code, 422)
        self.assertEqual(unsupported.json()["detail"]["code"], "AI_PROVIDER_UNSUPPORTED")

    def test_secret_shaped_model_values_and_extra_credentials_are_rejected(self):
        secret = self.client.put("/profiles/fixture/ai-task-settings", json={
            "tasks": {"job_scoring": {"provider": "ollama", "model": "sk-private-token"}},
        })
        self.assertEqual(secret.status_code, 422)
        self.assertEqual(secret.json()["detail"]["code"], "AI_MODEL_ID_INVALID")
        self.assertNotIn("sk-private-token", secret.text)

        credential_field = self.client.put("/profiles/fixture/ai-task-settings", json={
            "tasks": {"job_scoring": {"provider": "ollama", "model": "llama3.2:3b",
                                       "api_key": "private-token"}},
        })
        self.assertEqual(credential_field.status_code, 422)
        self.assertNotIn("private-token", credential_field.text)

        malformed = self.client.put("/profiles/fixture/ai-task-settings", json={
            "tasks": "sk-sensitive-malformed-body",
        })
        self.assertEqual(malformed.status_code, 422)
        self.assertNotIn("sk-sensitive-malformed-body", malformed.text)

    def test_unknown_profile_has_no_settings_resource(self):
        result = self.client.get("/profiles/not-a-profile/ai-task-settings")
        self.assertEqual(result.status_code, 404)

    def test_unsafe_provider_transport_cannot_change_saved_settings_or_audit(self):
        initial = self.client.get('/profiles/fixture/ai-task-settings').json()['tasks']
        with self.store.engine.connect() as connection:
            before_audit = list(connection.execute(select(m.audit.c.id)).scalars())
        for provider, endpoint_name in (
            ('openai-compatible', 'MJOBS_OPENAI_COMPATIBLE_BASE_URL'),
            ('openai', 'MJOBS_OPENAI_BASE_URL'),
            ('azure-openai', 'AZURE_OPENAI_ENDPOINT'),
        ):
            with self.subTest(provider=provider), patch.dict('os.environ', {
                endpoint_name: 'http://remote.example/v1',
            }, clear=True), patch('httpx.Client', side_effect=AssertionError('no provider I/O')):
                response = self.client.put('/profiles/fixture/ai-task-settings', json={
                    'tasks': {'job_scoring': {'provider': provider, 'model': 'synthetic-model'}},
                })
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()['detail'], {'code': 'AI_PROVIDER_CONFIGURATION_INVALID'})
            self.assertNotIn('remote.example', response.text)
            self.assertEqual(self.client.get('/profiles/fixture/ai-task-settings').json()['tasks'], initial)
        with self.store.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(m.audit.c.id)).scalars()), before_audit)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        with TestClient(create_app(self.store, 'http://127.0.0.1:8090', self.root,
                'redis://127.0.0.1:1/0', operation_mode='PERSONAL')) as client:
            self.assertEqual(client.get('/profiles/fixture/ai-task-settings').json()['tasks'], initial)


if __name__ == "__main__":
    unittest.main()
