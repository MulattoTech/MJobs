"""Fail-closed offline completeness check for every declared AI provider/task pair.

The fixture matrix tests routing, real prompt producer contracts and strict output
schemas using an in-process fake provider. It never invokes a live model or network.
Caller-provided prompt fixtures do not claim a shipped default prompt or end-to-end
product capability; the receipt identifies them separately.
"""
from __future__ import annotations

import argparse
import json
from itertools import product
from pathlib import Path
from typing import Any

from services.ai.consumer import AIJobEvaluation, AIJobInput, _prompt
from services.ai.prompt_versioning import PromptRegistry
from services.ai.providers import (
    SUPPORTED_PROVIDER_NAMES,
    AzureOpenAIProvider,
    LocalEndpointProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderCapabilities,
)
from services.ai.structured_output import (
    CANONICAL_SCHEMAS,
    StrictAIResult,
    StructuredOutputError,
    output_schema_for,
    parse_structured_output,
)
from services.ai.tasks import AI_TASKS, AITaskConfiguration, AITaskRouter

MATRIX = Path(__file__).with_name("provider_task_matrix.json")
_SCHEMA_NAMES = {
    "job_parsing": "job_parsing",
    "job_scoring": "ai_job_evaluation",
    "requirement_extraction": "requirement_classification",
    "resume_tailoring": "text",
    "cover_letters": "text",
    "free_form_answers": "text",
    "company_summarization": "company_summary",
}
_REGISTRY_TARGETS = {
    "job_parsing": "job_parsing",
    "resume_tailoring": "resume",
    "cover_letters": "cover_letter",
    "free_form_answers": "screening_answers",
}
_CALLER_FIXTURE_TASKS = frozenset({"requirement_extraction", "company_summarization"})
_PROVIDER_CLASSES = {
    "ollama": OllamaProvider,
    "openai": OpenAIProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "local": LocalEndpointProvider,
    "azure-openai": AzureOpenAIProvider,
    "azure": AzureOpenAIProvider,
}
_FORBIDDEN_FIXTURE_TEXT = ("sk-", "ghp_", "bearer ", "password=", "api_key=")


class MatrixError(ValueError):
    """Only stable CI classifications, never input or generated text."""


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise MatrixError("MATRIX_INPUT_INVALID") from None
    if not isinstance(value, dict):
        raise MatrixError("MATRIX_INPUT_INVALID")
    return value


def _names(values: Any, *, code: str) -> list[str]:
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))):
        raise MatrixError(code)
    return values


def _model_for(schema_name: str) -> type[StrictAIResult] | None:
    if schema_name == "text":
        return None
    if schema_name == "ai_job_evaluation":
        return AIJobEvaluation
    return CANONICAL_SCHEMAS.get(schema_name)


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, str):
        return any(marker in value.casefold() for marker in _FORBIDDEN_FIXTURE_TEXT)
    if isinstance(value, dict):
        return any(_contains_forbidden(key) or _contains_forbidden(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    return False


def _prompt_for(entry: dict[str, Any], registry: PromptRegistry) -> str:
    task = entry["task"]
    origin, data = entry.get("prompt_origin"), entry.get("prompt_data")
    if not isinstance(data, dict):
        raise MatrixError("TASK_PROMPT_FIXTURE_INVALID")
    if task in _REGISTRY_TARGETS:
        if (origin != "registry" or entry.get("prompt_target") != _REGISTRY_TARGETS[task]
                or set(entry) != {
                    "task", "prompt_origin", "prompt_target", "prompt_data", "schema", "result",
                }):
            raise MatrixError("TASK_PROMPT_ORIGIN_INVALID")
        target = _REGISTRY_TARGETS[task]
        version = registry.get_active_version(target)
        if (set(data) != set(version.variables)
                or any(not isinstance(value, str) for value in data.values())):
            raise MatrixError("TASK_PROMPT_FIXTURE_INVALID")
        try:
            prompt, _ = registry.render(target, **data)
        except (TypeError, ValueError):
            raise MatrixError("TASK_PROMPT_FIXTURE_INVALID") from None
    elif task == "job_scoring":
        if (origin != "consumer" or "prompt_target" in entry
                or set(entry) != {
                    "task", "prompt_origin", "prompt_data", "schema", "result",
                }):
            raise MatrixError("TASK_PROMPT_ORIGIN_INVALID")
        try:
            prompt = _prompt(AIJobInput(**data))
        except (TypeError, ValueError):
            raise MatrixError("TASK_PROMPT_FIXTURE_INVALID") from None
    elif task in _CALLER_FIXTURE_TASKS:
        if (origin != "caller_fixture" or "prompt_target" in entry
                or set(entry) != {
                    "task", "prompt_origin", "prompt_data", "schema", "result",
                } or set(data) != {"prompt"}):
            raise MatrixError("TASK_PROMPT_ORIGIN_INVALID")
        prompt = data["prompt"]
    else:
        raise MatrixError("TASK_PROMPT_ORIGIN_INVALID")
    if (not isinstance(prompt, str) or not 1 <= len(prompt) <= 12_000
            or not prompt.strip()
            or any(marker in prompt.casefold() for marker in _FORBIDDEN_FIXTURE_TEXT)):
        raise MatrixError("TASK_PROMPT_FIXTURE_INVALID")
    return prompt


def _result_for(entry: dict[str, Any], model: type[StrictAIResult] | None) -> str:
    result = entry.get("result")
    if _contains_forbidden(result):
        raise MatrixError("TASK_RESULT_FIXTURE_INVALID")
    if model is None:
        if not isinstance(result, str) or not result.strip() or len(result) > 4_000:
            raise MatrixError("TASK_RESULT_FIXTURE_INVALID")
        return result
    if not isinstance(result, dict):
        raise MatrixError("TASK_RESULT_FIXTURE_INVALID")
    try:
        raw = json.dumps(result, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
        parse_structured_output(raw, model)
    except (TypeError, ValueError, StructuredOutputError):
        raise MatrixError("TASK_RESULT_FIXTURE_INVALID") from None
    return raw


class _FixtureProvider:
    """Offline call recorder, never an HTTP provider or production AI model."""

    capabilities = ProviderCapabilities(text_generation=True, structured_output=True)

    def __init__(self, raw: str):
        self.raw = raw
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, task: str, model: str | None, prompt: str,
                 timeout_seconds: float, response_schema=None, cancellation_event=None) -> str:
        self.calls.append({
            "task": task, "model": model, "prompt": prompt,
            "timeout_seconds": timeout_seconds, "response_schema": response_schema,
            "cancellation_event": cancellation_event,
        })
        return self.raw


def _fixture_factory(expected: str, stub: _FixtureProvider):
    def select(name: str) -> _FixtureProvider:
        if name != expected:
            raise MatrixError("MATRIX_ROUTING_PROVIDER_MISMATCH")
        return stub
    return select


def validate_matrix(payload: dict[str, Any]) -> dict[str, Any]:
    """Require exactly one tested pairing for every declared enabled provider/task."""

    if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "fixture_kind", "providers", "tasks", "combinations",
    } or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise MatrixError("MATRIX_FORMAT_INVALID")
    if payload["fixture_kind"] != "synthetic_offline_only":
        raise MatrixError("MATRIX_FIXTURE_SCOPE_INVALID")
    if set(_SCHEMA_NAMES) != set(AI_TASKS):
        raise MatrixError("TASK_SCHEMA_POLICY_INCOMPLETE")
    providers = _names(payload["providers"], code="MATRIX_PROVIDERS_INVALID")
    expected_providers = set(SUPPORTED_PROVIDER_NAMES) - {"disabled"}
    if set(providers) != expected_providers:
        raise MatrixError("MATRIX_PROVIDER_COVERAGE_INCOMPLETE")
    if set(_PROVIDER_CLASSES) != expected_providers:
        raise MatrixError("MATRIX_PROVIDER_REGISTRY_INCOMPLETE")
    for provider_name in providers:
        capabilities = getattr(_PROVIDER_CLASSES[provider_name], "capabilities", None)
        if (not isinstance(capabilities, ProviderCapabilities)
                or capabilities.text_generation is not True
                or capabilities.structured_output is not True):
            raise MatrixError("MATRIX_PROVIDER_CAPABILITY_INCOMPLETE")
    entries = payload["tasks"]
    if (not isinstance(entries, list) or not entries
            or any(not isinstance(entry, dict) or not isinstance(entry.get("task"), str)
                   for entry in entries)):
        raise MatrixError("MATRIX_TASKS_INVALID")
    tasks = [entry["task"] for entry in entries]
    if len(tasks) != len(set(tasks)) or set(tasks) != set(AI_TASKS):
        raise MatrixError("MATRIX_TASK_COVERAGE_INCOMPLETE")
    combinations = payload["combinations"]
    if (not isinstance(combinations, list) or any(
            not isinstance(pair, dict) or set(pair) != {"provider", "task"}
            or not isinstance(pair.get("provider"), str)
            or not isinstance(pair.get("task"), str) for pair in combinations)):
        raise MatrixError("MATRIX_PAIRS_INVALID")
    pairs = [(pair["provider"], pair["task"]) for pair in combinations]
    if len(pairs) != len(set(pairs)) or set(pairs) != set(product(providers, tasks)):
        raise MatrixError("MATRIX_PAIR_COVERAGE_INCOMPLETE")

    registry = PromptRegistry()
    fixtures: dict[str, tuple[str, str, type[StrictAIResult] | None]] = {}
    for entry in entries:
        task = entry["task"]
        schema_name = entry.get("schema")
        if schema_name != _SCHEMA_NAMES[task]:
            raise MatrixError("TASK_SCHEMA_CONTRACT_MISMATCH")
        model = _model_for(schema_name)
        if schema_name != "text" and model is None:
            raise MatrixError("TASK_SCHEMA_CONTRACT_MISMATCH")
        prompt = _prompt_for(entry, registry)
        raw = _result_for(entry, model)
        fixtures[task] = (prompt, raw, model)

    structured = 0
    for provider_name, task in pairs:
        prompt, raw, model = fixtures[task]
        stub = _FixtureProvider(raw)
        configuration = AITaskConfiguration.from_mapping({
            task: {"provider": provider_name, "model": "fixture-model"},
        })
        router = AITaskRouter(
            configuration, provider_factory=_fixture_factory(provider_name, stub),
        )
        if model is None:
            if router.generate(task=task, prompt=prompt, timeout_seconds=1) != raw:
                raise MatrixError("MATRIX_TEXT_RESULT_MISMATCH")
            schema = None
        else:
            parsed = router.generate_structured(
                task=task, prompt=prompt, timeout_seconds=1, result_model=model,
            )
            if parsed.model_dump() != json.loads(raw):
                raise MatrixError("MATRIX_STRUCTURED_RESULT_MISMATCH")
            schema = output_schema_for(model)
            structured += 1
        if len(stub.calls) != 1 or stub.calls[0] != {
                "task": task, "model": "fixture-model", "prompt": prompt,
                "timeout_seconds": 1, "response_schema": schema,
                "cancellation_event": None,
        }:
            raise MatrixError("MATRIX_ROUTING_CONTRACT_MISMATCH")
    return {
        "schema_version": 1, "status": "PASS", "provider_count": len(providers),
        "task_count": len(tasks), "combination_count": len(pairs),
        "structured_combinations": structured,
        "text_combinations": len(pairs) - structured,
        "caller_fixture_only_tasks": sorted(_CALLER_FIXTURE_TASKS),
        "live_model_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=MATRIX)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = validate_matrix(_object(args.matrix))
        code = 0
    except MatrixError as error:
        receipt = {"schema_version": 1, "status": "ERROR", "error_code": str(error)}
        code = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
