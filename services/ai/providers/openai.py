"""OpenAI AI provider with bounded execution and telemetry integration."""

from __future__ import annotations

import os
from typing import Callable

import httpx

from services.ai.providers import ProviderCapabilities
from services.ai.providers.openai_compatible import OpenAICompatibleProvider


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider(OpenAICompatibleProvider):
    """Direct OpenAI API provider with bounded requests and token telemetry."""

    name = "openai"
    capabilities = ProviderCapabilities(text_generation=True, structured_output=True, streaming=False)

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("MJOBS_OPENAI_API_KEY")
        super().__init__(
            base_url=base_url,
            api_key=resolved_key,
            client_factory=client_factory,
        )

    @classmethod
    def from_environment(cls) -> "OpenAIProvider":
        url = os.environ.get("MJOBS_OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL)
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("MJOBS_OPENAI_API_KEY")
        return cls(api_key=key, base_url=url)


__all__ = ["OpenAIProvider"]
