"""Profile-scoped AI task settings persist without storing credentials."""
import copy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
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

    def test_unknown_tasks_and_unsupported_providers_fail_closed(self):
        unknown_task = self.client.put("/profiles/fixture/ai-task-settings", json={
            "tasks": {"private-task": {"provider": "disabled", "model": None}},
        })
        self.assertEqual(unknown_task.status_code, 422)
        self.assertEqual(unknown_task.json()["detail"]["code"], "AI_TASK_UNSUPPORTED")
        self.assertNotIn("private-task", unknown_task.text)

        unsupported = self.client.put("/profiles/fixture/ai-task-settings", json={
            "tasks": {"job_scoring": {"provider": "openai", "model": "gpt-test"}},
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


if __name__ == "__main__":
    unittest.main()
