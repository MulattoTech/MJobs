"""Independent provider/model selection for each AI task."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Callable, Mapping

from services.ai.providers import AIProvider, create_provider


class AITask(StrEnum):
    JOB_PARSING = "job_parsing"
    JOB_SCORING = "job_scoring"
    REQUIREMENT_EXTRACTION = "requirement_extraction"
    RESUME_TAILORING = "resume_tailoring"
    COVER_LETTERS = "cover_letters"
    FREE_FORM_ANSWERS = "free_form_answers"
    COMPANY_SUMMARIZATION = "company_summarization"


AI_TASKS = tuple(task.value for task in AITask)
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SAFE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_SECRET_PREFIXES = ("sk-", "sk_", "ghp_", "gho_", "hf_")


@dataclass(frozen=True)
class TaskModelSelection:
    """Non-secret provider/model identifiers for one AI task."""

    provider: str = "disabled"
    model: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not _SAFE_NAME.fullmatch(self.provider):
            raise ValueError("AI_PROVIDER_NAME_INVALID")
        if self.model is None:
            if self.provider != "disabled":
                raise ValueError("AI_MODEL_ID_REQUIRED")
            return
        if (self.provider == "disabled" or not isinstance(self.model, str)
                or not _SAFE_MODEL.fullmatch(self.model)
                or self.model.casefold().startswith(_SECRET_PREFIXES)):
            raise ValueError("AI_MODEL_ID_INVALID")


@dataclass(frozen=True)
class AITaskConfiguration:
    """Immutable complete mapping; omitted tasks remain explicitly disabled."""

    _selections: Mapping[str, TaskModelSelection]

    def __post_init__(self) -> None:
        if (not isinstance(self._selections, Mapping)
                or set(self._selections) != set(AI_TASKS)
                or any(not isinstance(value, TaskModelSelection)
                       for value in self._selections.values())):
            raise ValueError("AI_TASK_CONFIG_INVALID")
        object.__setattr__(self, "_selections", MappingProxyType(dict(self._selections)))

    @classmethod
    def default(cls) -> "AITaskConfiguration":
        return cls.from_mapping({})

    @classmethod
    def from_mapping(cls, configured: Mapping[str, object]) -> "AITaskConfiguration":
        if not isinstance(configured, Mapping):
            raise ValueError("AI_TASK_CONFIG_INVALID")
        unknown = set(configured) - set(AI_TASKS)
        if unknown:
            raise ValueError("AI_TASK_UNSUPPORTED")

        selections = {task: TaskModelSelection() for task in AI_TASKS}
        for task, value in configured.items():
            if not isinstance(value, Mapping) or set(value) - {"provider", "model"}:
                raise ValueError("AI_TASK_CONFIG_INVALID")
            try:
                selections[task] = TaskModelSelection(
                    provider=value.get("provider", "disabled"),
                    model=value.get("model"),
                )
            except (TypeError, ValueError) as error:
                raise ValueError(str(error)) from None
        return cls(MappingProxyType(selections))

    @property
    def tasks(self) -> tuple[str, ...]:
        return AI_TASKS

    def for_task(self, task: str | AITask) -> TaskModelSelection:
        if not isinstance(task, str) or task not in self._selections:
            raise ValueError("AI_TASK_UNSUPPORTED")
        return self._selections[task]


class AITaskRouter:
    """Pass each task's model choice to its selected, explicitly supported provider."""

    def __init__(self, configuration: AITaskConfiguration | None = None,
                 provider_factory: Callable[[str], AIProvider] = create_provider) -> None:
        self.configuration = configuration or AITaskConfiguration.default()
        self.provider_factory = provider_factory

    def generate(self, *, task: str | AITask, prompt: str, timeout_seconds: float) -> str:
        selection = self.configuration.for_task(task)
        provider = self.provider_factory(selection.provider)
        return provider.generate(
            task=str(task), model=selection.model, prompt=prompt,
            timeout_seconds=timeout_seconds,
        )


__all__ = ["AI_TASKS", "AITask", "AITaskConfiguration", "AITaskRouter", "TaskModelSelection"]
