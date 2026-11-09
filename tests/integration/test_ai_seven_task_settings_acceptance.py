"""MJ-REQ-109: all seven model selections persist independently per profile.

Synthetic model IDs exercise configuration, not model availability or inference.
"""

import copy
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


# Keep the acceptance inventory independent of the implementation's AI_TASKS.
TASK_LABELS = (
    ("job_parsing", "Job parsing"),
    ("job_scoring", "Job scoring"),
    ("requirement_extraction", "Requirement extraction"),
    ("resume_tailoring", "Resume tailoring"),
    ("cover_letters", "Cover letters"),
    ("free_form_answers", "Free-form answers"),
    ("company_summarization", "Company summarization"),
)
FIRST_PROFILE = "seven-task-first"
SECOND_PROFILE = "seven-task-second"


def disabled_selections():
    return {task: {"provider": "disabled", "model": None} for task, _ in TASK_LABELS}


def model_selections(profile, revision=1):
    return {
        task: {"provider": "ollama", "model":
               f"synthetic-{profile}/{task.replace('_', '-')}:v{revision}"}
        for task, _ in TASK_LABELS
    }


def seed_profiles(store):
    for profile_id, name in ((FIRST_PROFILE, "Seven Task Alpha"),
                             (SECOND_PROFILE, "Seven Task Beta")):
        profile = copy.deepcopy(PROFILE)
        profile.update(id=profile_id, name=name, email=f"{profile_id}@example.test")
        store.save_profile(profile)


@contextmanager
def forbid_provider_requests():
    """Keep real provider validation while rejecting generation and HTTP I/O."""
    with patch.dict("os.environ", {"MJOBS_OLLAMA_BASE_URL": "http://127.0.0.1:11434"}), \
            patch("services.ai.providers.ollama.OllamaProvider.generate",
                  side_effect=AssertionError("settings must not generate")) as generate, \
            patch("httpx.HTTPTransport.handle_request",
                  side_effect=AssertionError("settings must not perform provider HTTP I/O")) as sync_io, \
            patch("httpx.AsyncHTTPTransport.handle_async_request",
                  side_effect=AssertionError("settings must not perform provider HTTP I/O")) as async_io:
        yield (generate, sync_io, async_io)


class SevenTaskSettingsAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-seven-task-settings-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database_url = f"sqlite:///{(self.root / 'settings.db').as_posix()}"
        self.store = Store(self.database_url)
        self.addCleanup(lambda: self.store.engine.dispose())
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        seed_profiles(self.store)
        self.client = self._client()
        self.addCleanup(lambda: self.client.close())

    def _client(self):
        return TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))

    def _reopen(self):
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()

    def _read(self, profile_id, expected):
        response = self.client.get(f"/profiles/{profile_id}/ai-task-settings")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["profile_id"], profile_id)
        self.assertEqual(response.json()["tasks"], expected)

    def _save(self, profile_id, selections):
        response = self.client.put(f"/profiles/{profile_id}/ai-task-settings",
                                   json={"tasks": selections})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["profile_id"], profile_id)
        self.assertEqual(response.json()["tasks"], selections)
        self.assertEqual(self.store.ai_task_settings(profile_id), selections)

    def test_seven_distinct_models_survive_save_and_reopen_for_two_profiles(self):
        first, second = model_selections("alpha"), model_selections("beta")
        self.assertEqual(len({value["model"] for value in first.values()}), 7)
        self.assertEqual(len({value["model"] for value in second.values()}), 7)
        with forbid_provider_requests() as guards:
            self._read(FIRST_PROFILE, disabled_selections())
            self._read(SECOND_PROFILE, disabled_selections())
            self._save(FIRST_PROFILE, first)
            self._read(SECOND_PROFILE, disabled_selections())
            self._save(SECOND_PROFILE, second)
            self._read(FIRST_PROFILE, first)

            # A fresh Store and app must read durable rows, not an in-memory map.
            self._reopen()
            self._read(FIRST_PROFILE, first)
            self._read(SECOND_PROFILE, second)
            self.assertEqual(self.store.ai_task_settings(FIRST_PROFILE), first)
            self.assertEqual(self.store.ai_task_settings(SECOND_PROFILE), second)
            for guard in guards:
                guard.assert_not_called()

    def test_each_task_can_change_and_disable_without_overwriting_other_selections(self):
        first, second = model_selections("alpha"), model_selections("beta")
        updated = model_selections("alpha", revision=2)
        with forbid_provider_requests() as guards:
            self._save(FIRST_PROFILE, first)
            self._save(SECOND_PROFILE, second)
            for task, _ in TASK_LABELS:
                with self.subTest(task=task, action="change model"):
                    first[task] = updated[task]
                    self._save(FIRST_PROFILE, first)
                    self._read(FIRST_PROFILE, first)
                    self._read(SECOND_PROFILE, second)
            self._reopen()
            self._read(FIRST_PROFILE, updated)
            self._read(SECOND_PROFILE, second)

            for task, _ in TASK_LABELS:
                with self.subTest(task=task, action="disable"):
                    first[task] = {"provider": "disabled", "model": None}
                    self._save(FIRST_PROFILE, first)
                    self._read(FIRST_PROFILE, first)
                    self._read(SECOND_PROFILE, second)
            self._reopen()
            self._read(FIRST_PROFILE, disabled_selections())
            self._read(SECOND_PROFILE, second)
            for guard in guards:
                guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
