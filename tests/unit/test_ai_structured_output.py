"""AI results are accepted only through explicit strict result schemas."""
import socket
import unittest
from unittest.mock import patch


class AIStructuredOutputTests(unittest.TestCase):
    def test_parser_returns_a_typed_result_from_valid_schema_checked_json(self):
        from services.ai.structured_output import StrictAIResult, parse_structured_output

        class Score(StrictAIResult):
            score: int
            rationale: str

        result = parse_structured_output(
            '{"score":91,"rationale":"Matches required experience"}', Score,
        )

        self.assertIsInstance(result, Score)
        self.assertEqual(result.score, 91)
        self.assertEqual(result.rationale, "Matches required experience")

    def test_parser_rejects_malformed_ambiguous_and_schema_invalid_json_safely(self):
        from services.ai.structured_output import (
            StrictAIResult, StructuredOutputError, parse_structured_output,
        )

        class Score(StrictAIResult):
            score: int
            rationale: str

        class Skill(StrictAIResult):
            name: str

        class Match(StrictAIResult):
            skills: list[Skill]

        invalid = (
            '{"score":true,"rationale":"PRIVATE_RESULT"}',
            '{"score":91,"rationale":"ok","extra":"PRIVATE_RESULT"}',
            '{"score":91,"score":92,"rationale":"PRIVATE_RESULT"}',
            '{"score":91,"rationale":"PRIVATE_RESULT"',
            '[{"score":91,"rationale":"PRIVATE_RESULT"}]',
            '{"score":NaN,"rationale":"PRIVATE_RESULT"}',
            '{"rationale":"PRIVATE_RESULT"}',
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(StructuredOutputError) as error:
                parse_structured_output(raw, Score)
            self.assertEqual(error.exception.code, "AI_STRUCTURED_OUTPUT_INVALID")
            self.assertNotIn("PRIVATE_RESULT", str(error.exception))
            self.assertNotIn(raw, str(error.exception))

        with self.assertRaises(StructuredOutputError) as nested_extra:
            parse_structured_output(
                '{"skills":[{"name":"PowerShell","private":"PRIVATE_RESULT"}]}',
                Match,
            )
        self.assertEqual(nested_extra.exception.code, "AI_STRUCTURED_OUTPUT_INVALID")
        self.assertNotIn("PRIVATE_RESULT", str(nested_extra.exception))

        self.assertNotIn("PRIVATE_RESULT", str(nested_extra.exception))

    def test_parser_rejects_unbounded_text_and_non_strict_schema_contracts(self):
        from pydantic import BaseModel, ConfigDict

        from services.ai.structured_output import (
            MAX_STRUCTURED_OUTPUT_CHARACTERS, StrictAIResult,
            StructuredOutputError, parse_structured_output,
        )

        class Score(StrictAIResult):
            score: int

        class LaxScore(BaseModel):
            model_config = ConfigDict(extra="ignore")
            score: int

        class LaxOverride(StrictAIResult):
            model_config = ConfigDict(extra="ignore")
            score: int

        class LaxNested(BaseModel):
            score: int

        class NestedScore(StrictAIResult):
            nested: LaxNested

        with self.assertRaises(StructuredOutputError) as too_large:
            parse_structured_output(" " * (MAX_STRUCTURED_OUTPUT_CHARACTERS + 1), Score)
        self.assertEqual(too_large.exception.code, "AI_STRUCTURED_OUTPUT_TOO_LARGE")

        with self.assertRaises(StructuredOutputError) as lax_schema:
            parse_structured_output('{"score":1}', LaxScore)
        self.assertEqual(lax_schema.exception.code, "AI_STRUCTURED_SCHEMA_INVALID")

        with self.assertRaises(StructuredOutputError) as lax_override:
            parse_structured_output('{"score":1}', LaxOverride)
        self.assertEqual(lax_override.exception.code, "AI_STRUCTURED_SCHEMA_INVALID")

        with self.assertRaises(StructuredOutputError) as lax_nested:
            parse_structured_output('{"nested":{"score":1}}', NestedScore)
        self.assertEqual(lax_nested.exception.code, "AI_STRUCTURED_SCHEMA_INVALID")

    def test_task_router_requests_and_validates_schema_capable_provider_output(self):
        from services.ai.providers import ProviderCapabilities
        from services.ai.structured_output import StrictAIResult
        from services.ai.tasks import AITaskConfiguration, AITaskRouter

        class Score(StrictAIResult):
            score: int
            rationale: str

        calls = []

        class StructuredProvider:
            capabilities = ProviderCapabilities(structured_output=True)

            def generate(self, *, task, model, prompt, timeout_seconds, response_schema=None):
                calls.append((task, model, prompt, timeout_seconds, response_schema))
                return '{"score":88,"rationale":"synthetic match"}'

        configuration = AITaskConfiguration.from_mapping({
            "job_scoring": {"provider": "openai", "model": "test-model"},
        })
        router = AITaskRouter(configuration, provider_factory=lambda name: StructuredProvider())

        result = router.generate_structured(
            task="job_scoring", prompt="synthetic prompt", timeout_seconds=2,
            result_model=Score,
        )

        self.assertIsInstance(result, Score)
        self.assertEqual(result.score, 88)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ("job_scoring", "test-model", "synthetic prompt", 2))
        self.assertEqual(calls[0][4]["properties"]["score"]["type"], "integer")

    def test_task_router_refuses_unstructured_provider_before_generation(self):
        from services.ai.providers import ProviderCapabilities
        from services.ai.structured_output import StrictAIResult, StructuredOutputError
        from services.ai.tasks import AITaskConfiguration, AITaskRouter

        class Score(StrictAIResult):
            score: int

        class TextOnlyProvider:
            capabilities = ProviderCapabilities(text_generation=True)
            calls = 0

            def generate(self, **kwargs):
                self.calls += 1
                return '{"score":1}'

        provider = TextOnlyProvider()
        router = AITaskRouter(
            AITaskConfiguration.from_mapping({
                "job_scoring": {"provider": "openai", "model": "test-model"},
            }),
            provider_factory=lambda name: provider,
        )

        with self.assertRaises(StructuredOutputError) as error:
            router.generate_structured(
                task="job_scoring", prompt="PRIVATE_PROMPT", timeout_seconds=1,
                result_model=Score,
            )

        self.assertEqual(error.exception.code, "AI_STRUCTURED_OUTPUT_UNSUPPORTED")
        self.assertNotIn("PRIVATE_PROMPT", str(error.exception))
        self.assertEqual(provider.calls, 0)

    def test_task_router_sanitizes_provider_failure_details(self):
        from services.ai.providers import ProviderCapabilities
        from services.ai.structured_output import StrictAIResult, StructuredOutputError
        from services.ai.tasks import AITaskConfiguration, AITaskRouter

        class Score(StrictAIResult):
            score: int

        class FailingProvider:
            capabilities = ProviderCapabilities(structured_output=True)

            def generate(self, **kwargs):
                raise RuntimeError("PRIVATE_PROMPT PRIVATE_PROVIDER_RESPONSE")

        router = AITaskRouter(
            AITaskConfiguration.from_mapping({
                "job_scoring": {"provider": "openai", "model": "test-model"},
            }),
            provider_factory=lambda name: FailingProvider(),
        )

        with self.assertRaises(StructuredOutputError) as error:
            router.generate_structured(
                task="job_scoring", prompt="PRIVATE_PROMPT", timeout_seconds=1,
                result_model=Score,
            )

        self.assertEqual(error.exception.code, "AI_STRUCTURED_PROVIDER_FAILED")
        self.assertNotIn("PRIVATE_PROMPT", str(error.exception))
        self.assertNotIn("PRIVATE_PROVIDER_RESPONSE", str(error.exception))

    def test_task_router_keeps_disabled_provider_closed_for_structured_requests(self):
        from services.ai.providers import AIProviderDisabled, ProviderCapabilities
        from services.ai.structured_output import StrictAIResult
        from services.ai.tasks import AITaskConfiguration, AITaskRouter

        class Score(StrictAIResult):
            score: int

        class MustNotGenerate:
            capabilities = ProviderCapabilities(structured_output=True)
            calls = 0

            def generate(self, **kwargs):
                self.calls += 1
                return '{"score":1}'

        provider = MustNotGenerate()
        router = AITaskRouter(AITaskConfiguration.default(),
                              provider_factory=lambda name: provider)

        with self.assertRaises(AIProviderDisabled):
            router.generate_structured(
                task="job_scoring", prompt="private", timeout_seconds=1,
                result_model=Score,
            )
        self.assertEqual(provider.calls, 0)

    def test_task_router_fails_closed_for_unimplemented_provider_without_network(self):
        from services.ai.structured_output import StrictAIResult
        from services.ai.tasks import AITaskConfiguration, AITaskRouter

        class Score(StrictAIResult):
            score: int

        router = AITaskRouter(AITaskConfiguration.from_mapping({
            "job_scoring": {"provider": "openai", "model": "test-model"},
        }))
        with patch.object(socket, "create_connection",
                           side_effect=AssertionError("unexpected network")):
            with self.assertRaises(ValueError) as error:
                router.generate_structured(
                    task="job_scoring", prompt="private", timeout_seconds=1,
                    result_model=Score,
                )
        self.assertEqual(str(error.exception), "AI_PROVIDER_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
