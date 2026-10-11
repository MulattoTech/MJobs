"""Per-task AI routing keeps tasks independent and unsupported providers closed."""
import socket
import unittest
from unittest.mock import patch


class AITaskConfigurationTests(unittest.TestCase):
    def test_every_task_has_an_explicit_safe_no_ai_fallback(self):
        from services.ai.tasks import AI_TASKS, fallback_capabilities

        capabilities = fallback_capabilities()
        self.assertEqual(set(capabilities), set(AI_TASKS))
        self.assertEqual(capabilities["job_scoring"]["mode"], "DETERMINISTIC")
        self.assertEqual(capabilities["resume_tailoring"]["mode"], "DETERMINISTIC")
        self.assertEqual(capabilities["cover_letters"]["mode"], "DETERMINISTIC")
        self.assertEqual(capabilities["free_form_answers"]["mode"], "UNAVAILABLE")
        self.assertEqual(capabilities["company_summarization"]["mode"], "UNAVAILABLE")
        self.assertTrue(all(value["description"].strip() for value in capabilities.values()))

    def test_default_configuration_disables_every_task(self):
        from services.ai.providers import AIProviderDisabled
        from services.ai.tasks import AI_TASKS, AITaskConfiguration, AITaskRouter

        configuration = AITaskConfiguration.default()

        self.assertEqual(len(AI_TASKS), 7)
        self.assertEqual(set(configuration.tasks), set(AI_TASKS))
        for task in AI_TASKS:
            selection = configuration.for_task(task)
            self.assertEqual(selection.provider, "disabled")
            self.assertIsNone(selection.model)

        router = AITaskRouter(configuration)
        with patch.object(socket, "create_connection", side_effect=AssertionError("unexpected network")):
            with self.assertRaises(AIProviderDisabled):
                router.generate(task="job_scoring", prompt="private", timeout_seconds=1)

    def test_each_task_routes_its_own_provider_and_model(self):
        from services.ai.tasks import AI_TASKS, AITaskConfiguration, AITaskRouter

        mapping = {
            task: {"provider": "openai", "model": f"test-model-{index}"}
            for index, task in enumerate(AI_TASKS)
        }
        configuration = AITaskConfiguration.from_mapping(mapping)
        calls = []

        class RecordingProvider:
            def generate(self, *, task, model, prompt, timeout_seconds):
                calls.append((task, model, prompt, timeout_seconds))
                return f"result:{task}:{model}"

        router = AITaskRouter(configuration, provider_factory=lambda name: RecordingProvider())
        with patch.object(socket, "create_connection", side_effect=AssertionError("unexpected network")):
            for task in AI_TASKS:
                result = router.generate(task=task, prompt="synthetic", timeout_seconds=1)
                self.assertEqual(result, f"result:{task}:test-model-{AI_TASKS.index(task)}")

        self.assertEqual(
            calls,
            [(task, f"test-model-{AI_TASKS.index(task)}", "synthetic", 1) for task in AI_TASKS],
        )

    def test_unknown_task_is_rejected_without_echoing_input(self):
        from services.ai.tasks import AITaskConfiguration

        with self.assertRaises(ValueError) as error:
            AITaskConfiguration.default().for_task("private-task-name")

        self.assertEqual(str(error.exception), "AI_TASK_UNSUPPORTED")
        self.assertNotIn("private-task-name", str(error.exception))

    def test_model_identifiers_reject_secret_shaped_values_without_echoing(self):
        from services.ai.tasks import AITaskConfiguration

        with self.assertRaises(ValueError) as error:
            AITaskConfiguration.from_mapping({
                "job_scoring": {"provider": "openai", "model": "sk-live-private-token"},
            })

        self.assertEqual(str(error.exception), "AI_MODEL_ID_INVALID")
        self.assertNotIn("sk-live-private-token", str(error.exception))

    def test_enabled_provider_requires_a_model_identifier(self):
        from services.ai.tasks import AITaskConfiguration

        with self.assertRaises(ValueError) as error:
            AITaskConfiguration.from_mapping({
                "job_scoring": {"provider": "openai"},
            })

        self.assertEqual(str(error.exception), "AI_MODEL_ID_REQUIRED")

    def test_unimplemented_provider_fails_closed_before_generation(self):
        from services.ai.tasks import AITaskConfiguration, AITaskRouter

        configuration = AITaskConfiguration.from_mapping({
            "job_scoring": {"provider": "anthropic", "model": "model-x"},
        })
        router = AITaskRouter(configuration)
        with patch.object(socket, "create_connection", side_effect=AssertionError("unexpected network")):
            with self.assertRaises(ValueError) as error:
                router.generate(task="job_scoring", prompt="private prompt", timeout_seconds=1)

        self.assertEqual(str(error.exception), "AI_PROVIDER_UNSUPPORTED")
        self.assertNotIn("private prompt", str(error.exception))

    def test_configuration_serialization_and_roundtrip(self):
        from services.ai.tasks import AI_TASKS, AITaskConfiguration

        config = AITaskConfiguration.from_mapping({
            "job_scoring": {"provider": "openai", "model": "gpt-4o-mini"},
            "resume_tailoring": {"provider": "ollama", "model": "llama3.2"},
        })
        serialized = config.to_dict()

        self.assertEqual(len(serialized), len(AI_TASKS))
        self.assertEqual(serialized["job_scoring"], {"provider": "openai", "model": "gpt-4o-mini"})
        self.assertEqual(serialized["resume_tailoring"], {"provider": "ollama", "model": "llama3.2"})
        self.assertEqual(serialized["job_parsing"], {"provider": "disabled", "model": None})

        # Roundtrip reconstruction
        reconstructed = AITaskConfiguration.from_mapping(serialized)
        self.assertEqual(reconstructed.to_dict(), serialized)

    def test_with_update_returns_immutable_updated_configuration(self):
        from services.ai.tasks import AITaskConfiguration

        initial = AITaskConfiguration.default()
        updated = initial.with_update({
            "job_scoring": {"provider": "openai", "model": "gpt-4o-mini"},
        })

        self.assertEqual(initial.for_task("job_scoring").provider, "disabled")
        self.assertEqual(updated.for_task("job_scoring").provider, "openai")
        self.assertEqual(updated.for_task("job_scoring").model, "gpt-4o-mini")

        with self.assertRaises(ValueError) as error:
            initial.with_update({"invalid_task": {"provider": "openai", "model": "gpt-4o"}})
        self.assertEqual(str(error.exception), "AI_TASK_UNSUPPORTED")

    def test_router_get_task_summary_includes_fallbacks(self):
        from services.ai.tasks import AI_TASKS, AITaskConfiguration, AITaskRouter

        config = AITaskConfiguration.from_mapping({
            "job_scoring": {"provider": "openai", "model": "gpt-4o-mini"},
        })
        router = AITaskRouter(config)
        summary = router.get_task_summary()

        self.assertEqual(set(summary), set(AI_TASKS))
        self.assertEqual(summary["job_scoring"]["provider"], "openai")
        self.assertEqual(summary["job_scoring"]["model"], "gpt-4o-mini")
        self.assertEqual(summary["job_scoring"]["fallback_mode"], "DETERMINISTIC")
        self.assertEqual(summary["free_form_answers"]["provider"], "disabled")
        self.assertEqual(summary["free_form_answers"]["fallback_mode"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
