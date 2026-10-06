"""Strict, bounded parsing for untrusted AI-produced structured results."""

from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.errors import PydanticUserError


MAX_STRUCTURED_OUTPUT_CHARACTERS = 100_000
_ERROR_CODES = {
    "AI_STRUCTURED_OUTPUT_INVALID",
    "AI_STRUCTURED_PROVIDER_FAILED",
    "AI_STRUCTURED_OUTPUT_TOO_LARGE",
    "AI_STRUCTURED_OUTPUT_UNSUPPORTED",
    "AI_STRUCTURED_SCHEMA_INVALID",
}
ResultT = TypeVar("ResultT", bound="StrictAIResult")


class StrictAIResult(BaseModel):
    """Base for AI result schemas: strict values and no undeclared fields."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class StructuredOutputError(ValueError):
    """Safe structured-output failure that never contains model output details."""

    def __init__(self, code: str) -> None:
        safe_code = code if code in _ERROR_CODES else "AI_STRUCTURED_OUTPUT_INVALID"
        self.code = safe_code
        super().__init__(safe_code)


def _require_result_model(result_model: object) -> type[StrictAIResult]:
    if (not isinstance(result_model, type)
            or not issubclass(result_model, StrictAIResult)
            or result_model is StrictAIResult
            or result_model.model_config.get("strict") is not True
            or result_model.model_config.get("extra") != "forbid"):
        raise StructuredOutputError("AI_STRUCTURED_SCHEMA_INVALID")
    return result_model


def _schema_is_closed(value: object) -> bool:
    if isinstance(value, dict):
        schema_type = value.get("type")
        if ((schema_type == "object"
             or isinstance(schema_type, list) and "object" in schema_type)
                and value.get("additionalProperties") is not False):
            return False
        return all(_schema_is_closed(child) for child in value.values())
    if isinstance(value, list):
        return all(_schema_is_closed(child) for child in value)
    return True


def output_schema_for(result_model: object) -> dict[str, object]:
    """Return a schema only for an explicitly strict MJobs result model."""

    model = _require_result_model(result_model)
    try:
        schema = model.model_json_schema()
    except (TypeError, ValueError, PydanticUserError, RecursionError):
        raise StructuredOutputError("AI_STRUCTURED_SCHEMA_INVALID") from None
    if not _schema_is_closed(schema):
        raise StructuredOutputError("AI_STRUCTURED_SCHEMA_INVALID")
    return schema


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def parse_structured_output(raw: object, result_model: object) -> ResultT:
    """Parse one bounded JSON object into a strict typed result, failing closed."""

    model = _require_result_model(result_model)
    output_schema_for(model)
    if not isinstance(raw, str):
        raise StructuredOutputError("AI_STRUCTURED_OUTPUT_INVALID")
    if len(raw) > MAX_STRUCTURED_OUTPUT_CHARACTERS:
        raise StructuredOutputError("AI_STRUCTURED_OUTPUT_TOO_LARGE")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise ValueError("expected JSON object")
        return model.model_validate(value, strict=True)
    except Exception:
        raise StructuredOutputError("AI_STRUCTURED_OUTPUT_INVALID") from None


__all__ = [
    "MAX_STRUCTURED_OUTPUT_CHARACTERS",
    "StrictAIResult",
    "StructuredOutputError",
    "output_schema_for",
    "parse_structured_output",
]
