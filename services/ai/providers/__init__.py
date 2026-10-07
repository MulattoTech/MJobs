"""Provider-neutral contracts and safe provider selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, NoReturn, Protocol


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

    def generate(self, *, task: str, model: str | None = None, prompt: str,
                 timeout_seconds: float,
                 response_schema: Mapping[str, object] | None = None) -> str: ...


class AIProviderDisabled(RuntimeError):
    """Raised when code requests generation from the explicitly disabled provider."""

    code = "AI_PROVIDER_DISABLED"

    def __init__(self) -> None:
        # Deliberately omit task, prompt, and provider configuration from the error.
        super().__init__(self.code)


class AIProviderError(RuntimeError):
    """Safe provider failure containing only a stable result code."""

    _CODES = {
        "AI_PROVIDER_CONFIGURATION_INVALID",
        "AI_PROVIDER_REQUEST_INVALID",
        "AI_PROVIDER_RESPONSE_INVALID",
        "AI_PROVIDER_RESPONSE_TOO_LARGE",
        "AI_PROVIDER_TIMEOUT",
        "AI_PROVIDER_UNAVAILABLE",
        "AI_PROVIDER_HTTP_ERROR",
    }

    def __init__(self, code: str) -> None:
        self.code = code if code in self._CODES else "AI_PROVIDER_RESPONSE_INVALID"
        super().__init__(self.code)


class DisabledAIProvider:
    """No-op provider that declares no capabilities and never performs I/O."""

    name = "disabled"
    capabilities = ProviderCapabilities()

    def generate(self, *, task: str, model: str | None = None, prompt: str,
                 timeout_seconds: float,
                 response_schema: Mapping[str, object] | None = None) -> NoReturn:
        raise AIProviderDisabled()


def create_provider(name: str) -> AIProvider:
    """Return only explicitly supported providers; reject unknown values safely."""

    if name == "disabled":
        return DisabledAIProvider()
    if name == "ollama":
        return OllamaProvider.from_environment()
    raise ValueError("AI_PROVIDER_UNSUPPORTED")


from services.ai.providers.ollama import OllamaProvider


__all__ = [
    "AIProvider",
    "AIProviderDisabled",
    "AIProviderError",
    "DisabledAIProvider",
    "OllamaProvider",
    "ProviderCapabilities",
    "create_provider",
]
