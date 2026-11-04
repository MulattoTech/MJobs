"""MJ-REF-030: fail-closed offline provider × AI task fixture matrix."""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from services.ai.providers import SUPPORTED_PROVIDER_NAMES, create_provider
from services.ai.tasks import AI_TASKS
from tools.ci.provider_task_matrix import (
    MATRIX,
    MatrixError,
    main,
    validate_matrix,
)


class ProviderTaskMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = json.loads(MATRIX.read_text(encoding="utf-8"))

    def assert_invalid(self, payload, code):
        with self.assertRaises(MatrixError) as error:
            validate_matrix(payload)
        self.assertEqual(str(error.exception), code)

    def test_every_declared_provider_and_task_has_one_offline_fixture(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), \
                patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden")):
            receipt = validate_matrix(deepcopy(self.original))
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["provider_count"], 6)
        self.assertEqual(receipt["task_count"], len(AI_TASKS))
        self.assertEqual(receipt["combination_count"], 42)
        self.assertEqual(receipt["structured_combinations"], 24)
        self.assertEqual(receipt["text_combinations"], 18)
        self.assertEqual(receipt["live_model_calls"], 0)
        self.assertEqual(receipt["caller_fixture_only_tasks"],
                         ["company_summarization", "requirement_extraction"])
        self.assertEqual(set(self.original["providers"]),
                         set(SUPPORTED_PROVIDER_NAMES) - {"disabled"})

    def test_incomplete_or_ambiguous_provider_task_matrix_fails_closed(self):
        changes = (
            (lambda value: value["combinations"].pop(), "MATRIX_PAIR_COVERAGE_INCOMPLETE"),
            (lambda value: value["combinations"].append(value["combinations"][0]),
             "MATRIX_PAIR_COVERAGE_INCOMPLETE"),
            (lambda value: value["combinations"][0].update(task="not_a_task"),
             "MATRIX_PAIR_COVERAGE_INCOMPLETE"),
            (lambda value: value["providers"].pop(), "MATRIX_PROVIDER_COVERAGE_INCOMPLETE"),
            (lambda value: value["providers"].append("anthropic"),
             "MATRIX_PROVIDER_COVERAGE_INCOMPLETE"),
            (lambda value: value["providers"].append("openai"),
             "MATRIX_PROVIDERS_INVALID"),
            (lambda value: value["tasks"].pop(), "MATRIX_TASK_COVERAGE_INCOMPLETE"),
            (lambda value: value["tasks"].append(value["tasks"][0]),
             "MATRIX_TASK_COVERAGE_INCOMPLETE"),
        )
        for change, code in changes:
            with self.subTest(code=code):
                value = deepcopy(self.original)
                change(value)
                self.assert_invalid(value, code)

    def test_prompt_target_source_and_fixture_mismatch_fails_closed(self):
        cases = (
            (1, lambda row: row.update(prompt_origin="caller_fixture"),
             "TASK_PROMPT_ORIGIN_INVALID"),
            (0, lambda row: row.update(prompt_target="resume"),
             "TASK_PROMPT_ORIGIN_INVALID"),
            (3, lambda row: row["prompt_data"].pop("career_facts"),
             "TASK_PROMPT_FIXTURE_INVALID"),
            (2, lambda row: row["prompt_data"].update(prompt="sk-private-credential"),
             "TASK_PROMPT_FIXTURE_INVALID"),
            (6, lambda row: row["prompt_data"].update(extra="untrusted"),
             "TASK_PROMPT_ORIGIN_INVALID"),
            (1, lambda row: row["prompt_data"].update(deterministic_score=True),
             "TASK_PROMPT_FIXTURE_INVALID"),
        )
        for index, mutate, code in cases:
            with self.subTest(index=index, code=code):
                value = deepcopy(self.original)
                mutate(value["tasks"][index])
                self.assert_invalid(value, code)

    def test_schema_contract_and_output_fixture_are_strict(self):
        cases = (
            (0, lambda row: row.update(schema="text"), "TASK_SCHEMA_CONTRACT_MISMATCH"),
            (1, lambda row: row["result"].update(category="INVALID"),
             "TASK_RESULT_FIXTURE_INVALID"),
            (1, lambda row: row["result"].update(rationale="sk-synthetic-secret"),
             "TASK_RESULT_FIXTURE_INVALID"),
            (0, lambda row: row["result"].update(salary_min=True),
             "TASK_RESULT_FIXTURE_INVALID"),
            (2, lambda row: row["result"].update(private="PRIVATE"),
             "TASK_RESULT_FIXTURE_INVALID"),
            (5, lambda row: row.update(result=""), "TASK_RESULT_FIXTURE_INVALID"),
            (6, lambda row: row.update(result={"unrecognized":"schema"}),
             "TASK_RESULT_FIXTURE_INVALID"),
        )
        for index, mutate, code in cases:
            with self.subTest(index=index, code=code):
                value = deepcopy(self.original)
                mutate(value["tasks"][index])
                self.assert_invalid(value, code)

    def test_policy_version_and_undeclared_fields_fail_closed(self):
        value = deepcopy(self.original)
        value["schema_version"] = 2
        self.assert_invalid(value, "MATRIX_FORMAT_INVALID")
        value = deepcopy(self.original)
        value["notes"] = "extra"
        self.assert_invalid(value, "MATRIX_FORMAT_INVALID")
        value = deepcopy(self.original)
        value["fixture_kind"] = "live_provider"
        self.assert_invalid(value, "MATRIX_FIXTURE_SCOPE_INVALID")
        value = deepcopy(self.original)
        value["combinations"][0]["unreviewed"] = "extra"
        self.assert_invalid(value, "MATRIX_PAIRS_INVALID")

    def test_real_provider_capability_drift_fails_closed(self):
        from services.ai.providers import OllamaProvider, ProviderCapabilities

        for capabilities in (
            ProviderCapabilities(text_generation=True, structured_output=False),
            ProviderCapabilities(text_generation=False, structured_output=True),
        ):
            with self.subTest(capabilities=capabilities), \
                    patch.object(OllamaProvider, "capabilities", capabilities):
                self.assert_invalid(deepcopy(self.original),
                                    "MATRIX_PROVIDER_CAPABILITY_INCOMPLETE")

    def test_declared_provider_factory_routes_without_network(self):
        from services.ai.providers import (
            AzureOpenAIProvider,
            LocalEndpointProvider,
            OllamaProvider,
            OpenAICompatibleProvider,
            OpenAIProvider,
        )

        concrete = {
            "ollama": OllamaProvider,
            "openai": OpenAIProvider,
            "openai-compatible": OpenAICompatibleProvider,
            "local": LocalEndpointProvider,
            "azure-openai": AzureOpenAIProvider,
            "azure": AzureOpenAIProvider,
        }
        sentinels = {cls: object() for cls in set(concrete.values())}
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                socket.socket, "connect", side_effect=AssertionError("network forbidden"),
            ))
            for cls, sentinel in sentinels.items():
                stack.enter_context(patch.object(cls, "from_environment", return_value=sentinel))
            for provider in SUPPORTED_PROVIDER_NAMES:
                if provider == "disabled":
                    continue
                with self.subTest(provider=provider):
                    self.assertIs(create_provider(provider), sentinels[concrete[provider]])

    def test_unknown_provider_still_rejected_by_real_factory(self):
        with self.assertRaisesRegex(ValueError, "^AI_PROVIDER_UNSUPPORTED$"):
            create_provider("anthropic")
        with self.assertRaisesRegex(ValueError, "^AI_PROVIDER_UNSUPPORTED$"):
            create_provider("private-provider")

    def test_cli_emits_machine_readable_success_and_failure_receipts(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-matrix-fixture-") as root:
            folder = Path(root)
            success = folder / "ok.json"
            failure = folder / "failed.json"
            corrupted = folder / "invalid.json"
            self.assertEqual(main(["--matrix", str(MATRIX),
                                   "--output", str(success)]), 0)
            receipt = json.loads(success.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["combination_count"], 42)
            corrupted.write_text("PRIVATE_FIXTURE is not JSON", encoding="utf-8")
            self.assertEqual(main(["--matrix", str(corrupted),
                                   "--output", str(failure)]), 2)
            failed = json.loads(failure.read_text(encoding="utf-8"))
            self.assertEqual(failed, {
                "schema_version": 1, "status": "ERROR", "error_code": "MATRIX_INPUT_INVALID",
            })
            self.assertNotIn("PRIVATE_FIXTURE", failure.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
