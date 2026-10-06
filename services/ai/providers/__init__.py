"""Provider-neutral contracts and safe provider selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    """Explicit operations a provider supports; all default to unsupported."""

    text_generation: bool = False
    structured_output: bool = False
    streaming: bool = False


class AIProvider(Protocol):
    """Small provider contract shared by matching and document services."""

    name: str
    capabilities: ProviderCapabilities

    def generate(self, *, task: str, prompt: str, timeout_seconds: float) -> str: ...


class AIProviderDisabled(RuntimeError):
    """Raised when code requests generation from the explicitly disabled provider."""

    code = "AI_PROVIDER_DISABLED"

    def __init__(self) -> None:
        # Deliberately omit task, prompt, and provider configuration from the error.
        super().__init__(self.code)


class DisabledAIProvider:
    """No-op provider that declares no capabilities and never performs I/O."""

    name = "disabled"
    capabilities = ProviderCapabilities()

    def generate(self, *, task: str, prompt: str, timeout_seconds: float) -> NoReturn:
        raise AIProviderDisabled()


def create_provider(name: str) -> AIProvider:
    """Return only explicitly supported providers; reject unknown values safely."""

    if name == "disabled":
        return DisabledAIProvider()
    raise ValueError("AI_PROVIDER_UNSUPPORTED")


__all__ = [
    "AIProvider",
    "AIProviderDisabled",
    "DisabledAIProvider",
    "ProviderCapabilities",
    "create_provider",
]
