"""Typed provider-neutral tool catalog with explicit mutation classes."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Callable, Type

from pydantic import BaseModel, ValidationError


MAX_TOOL_ARGUMENT_BYTES = 8_192
MAX_TOOL_RESULT_BYTES = 8_192
MAX_REGISTERED_TOOLS = 64
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FORBIDDEN_SCHEMA_KEYS = {
    "store", "connection", "database", "filesystem", "browser", "network",
    "socket", "process", "command", "shell", "credential", "cookie",
}


class ToolClass(str, Enum):
    READ = "READ"
    PROPOSE = "PROPOSE"
    SIDE_EFFECT = "SIDE_EFFECT"


class AgentToolError(ValueError):
    """Safe tool-boundary failure containing only a stable result code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AgentToolContext:
    """Host-owned scope metadata; never serialized into model tool schemas."""

    run_id: str
    profile_id: str
    task: str
    proposal_id: str | None = None


@dataclass(frozen=True)
class ReviewDecision:
    reviewer_id: str
    approved: bool

    def __post_init__(self):
        if not isinstance(self.reviewer_id, str) or not self.reviewer_id.strip():
            raise ValueError("A reviewer identity is required")
        if type(self.approved) is not bool:
            raise ValueError("Review approval must be an explicit boolean")


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    result_code: str


ToolHandler = Callable[[BaseModel, AgentToolContext], object]
ToolPolicy = Callable[[BaseModel, AgentToolContext], PolicyDecision]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    classification: ToolClass
    input_model: Type[BaseModel]
    output_model: Type[BaseModel]
    handler: ToolHandler
    policy: ToolPolicy | None = None
    execution_model: Type[BaseModel] | None = None


def _schema_keys(value: object):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).lower()
            yield from _schema_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _schema_keys(nested)


class ToolRegistry:
    """An immutable-at-runtime allowlist of strict JSON tool contracts."""

    def __init__(self):
        self._tools: dict[str, AgentTool] = {}
        self._sealed = False

    def register(self, tool: AgentTool) -> None:
        if self._sealed:
            raise ValueError("Tool registry is sealed")
        if (not isinstance(tool, AgentTool) or not isinstance(tool.name, str)
                or not _TOOL_NAME.fullmatch(tool.name)
                or not isinstance(tool.description, str) or not tool.description.strip()
                or len(tool.description) > 500
                or not isinstance(tool.classification, ToolClass)):
            raise ValueError("Invalid tool definition")
        if tool.name in self._tools:
            raise ValueError("Duplicate tool name")
        if len(self._tools) >= MAX_REGISTERED_TOOLS:
            raise ValueError("Tool registry is full")
        for model in (tool.input_model, tool.output_model, tool.execution_model):
            if model is None:
                continue
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise ValueError("Tool schemas must be Pydantic models")
        if tool.input_model.model_config.get("extra") != "forbid" or tool.input_model.model_config.get("strict") is not True:
            raise ValueError("Tool input models must be strict and forbid extra fields")
        schema = tool.input_model.model_json_schema()
        if _FORBIDDEN_SCHEMA_KEYS.intersection(_schema_keys(schema)):
            raise ValueError("Tool input schema contains a host capability field")
        if not callable(tool.handler):
            raise ValueError("Tool handler must be callable")
        if tool.classification == ToolClass.SIDE_EFFECT and not callable(tool.policy):
            raise ValueError("Side-effect tools require a deterministic policy")
        if tool.classification != ToolClass.SIDE_EFFECT and tool.policy is not None:
            raise ValueError("Only side-effect tools may define an execution policy")
        self._tools[tool.name] = tool

    def seal(self) -> None:
        self._sealed = True

    def model_schemas(self) -> list[dict[str, object]]:
        """Return data-only schemas; implementation objects never cross this boundary."""
        self._sealed = True
        return [{
            "name": tool.name,
            "description": tool.description,
            "classification": tool.classification.value,
            "input_schema": tool.input_model.model_json_schema(),
            "output_schema": tool.output_model.model_json_schema(),
        } for tool in self._tools.values()]

    def get(self, name: str) -> AgentTool:
        if not isinstance(name, str) or name not in self._tools:
            raise AgentToolError("UNKNOWN_TOOL")
        return self._tools[name]

    @staticmethod
    def parse(tool: AgentTool, raw_arguments: object) -> tuple[BaseModel, str]:
        try:
            serialized = json.dumps(raw_arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, RecursionError) as error:
            raise AgentToolError("INVALID_ARGUMENTS") from error
        if len(serialized.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise AgentToolError("ARGUMENTS_TOO_LARGE")
        if not isinstance(raw_arguments, dict):
            raise AgentToolError("INVALID_ARGUMENTS")
        try:
            parsed = tool.input_model.model_validate(raw_arguments, strict=True)
        except (ValidationError, TypeError, ValueError) as error:
            raise AgentToolError("INVALID_ARGUMENTS") from error
        return parsed, serialized

    @staticmethod
    def output(tool: AgentTool, value: object) -> dict[str, Any]:
        try:
            parsed = tool.output_model.model_validate(value, strict=True)
            result = parsed.model_dump(mode="json")
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (ValidationError, TypeError, ValueError, RecursionError) as error:
            raise AgentToolError("INVALID_TOOL_RESULT") from error
        if len(encoded.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
            raise AgentToolError("RESULT_TOO_LARGE")
        return result

    def invoke(self, name: str, raw_arguments: object,
               context: AgentToolContext) -> tuple[dict[str, Any], str | None]:
        tool = self.get(name)
        parsed, canonical_arguments = self.parse(tool, raw_arguments)
        if tool.classification == ToolClass.SIDE_EFFECT:
            import hashlib
            proposal_id = hashlib.sha256(
                (tool.name + "\0" + context.run_id + "\0" + context.profile_id
                 + "\0" + context.task
                 + "\0" + canonical_arguments).encode("utf-8")
            ).hexdigest()
            proposal = {
                "proposal_id": proposal_id,
                "tool_name": tool.name,
                "classification": tool.classification.value,
                "arguments": parsed.model_dump(mode="json"),
                "review_required": True,
            }
            return self.output(tool, proposal), proposal_id
        try:
            output = tool.handler(parsed, context)
        except AgentToolError:
            raise
        except Exception as error:
            raise AgentToolError("TOOL_FAILED") from error
        return self.output(tool, output), None

    def proposal_id(self, name: str, raw_arguments: object,
                    context: AgentToolContext) -> str:
        tool = self.get(name)
        if tool.classification != ToolClass.SIDE_EFFECT:
            raise AgentToolError("TOOL_NOT_SIDE_EFFECT")
        _, canonical_arguments = self.parse(tool, raw_arguments)
        import hashlib
        return hashlib.sha256(
            (tool.name + "\0" + context.run_id + "\0" + context.profile_id
             + "\0" + context.task
             + "\0" + canonical_arguments).encode("utf-8")
        ).hexdigest()

    def execute_side_effect(self, name: str, raw_arguments: object,
                            context: AgentToolContext) -> dict[str, Any]:
        tool = self.get(name)
        if tool.classification != ToolClass.SIDE_EFFECT:
            raise AgentToolError("TOOL_NOT_SIDE_EFFECT")
        parsed, _ = self.parse(tool, raw_arguments)
        decision = tool.policy(parsed, context)
        if not isinstance(decision, PolicyDecision) or type(decision.allowed) is not bool:
            raise AgentToolError("INVALID_POLICY_DECISION")
        if not decision.allowed:
            raise AgentToolError("POLICY_DENIED")
        try:
            output = tool.handler(parsed, context)
        except AgentToolError:
            raise
        except Exception as error:
            raise AgentToolError("TOOL_FAILED") from error
        if tool.execution_model is None:
            return self.output(tool, output)
        execution_tool = AgentTool(
            name=tool.name, description=tool.description,
            classification=tool.classification, input_model=tool.input_model,
            output_model=tool.execution_model, handler=tool.handler,
            policy=tool.policy,
        )
        return self.output(execution_tool, output)
