"""Loopback-only local AI endpoint provider (LM Studio, LocalAI, vLLM) with strict SSRF defense."""

from __future__ import annotations

import ipaddress
import os
from typing import Callable
from urllib.parse import urlsplit

import httpx

from services.ai.providers import AIProviderError, ProviderCapabilities
from services.ai.providers.openai_compatible import OpenAICompatibleProvider


DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"


def _validate_loopback_base_url(value: object) -> str:
    """Validate that the local endpoint URL targets strictly a loopback IP address."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        address = ipaddress.ip_address(host) if host is not None else None
    except (ValueError, TypeError):
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID") from None

    if (
        parsed.scheme != "http"
        or address is None
        or not address.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")

    if port is not None and not 1 <= port <= 65535:
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")

    path = parsed.path.rstrip("/")
    return f"http://{parsed.netloc}{path}"


class LocalEndpointProvider(OpenAICompatibleProvider):
    """Loopback-only local OpenAI-compatible inference endpoint provider."""

    name = "local"
    capabilities = ProviderCapabilities(text_generation=True, structured_output=True, streaming=False)

    def __init__(
        self,
        base_url: str = DEFAULT_LOCAL_BASE_URL,
        api_key: str | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        validated_url = _validate_loopback_base_url(base_url)
        super().__init__(
            base_url=validated_url,
            api_key=api_key,
            client_factory=client_factory,
        )

    @classmethod
    def from_environment(cls) -> "LocalEndpointProvider":
        url = os.environ.get("MJOBS_LOCAL_AI_BASE_URL", DEFAULT_LOCAL_BASE_URL)
        key = os.environ.get("MJOBS_LOCAL_AI_API_KEY")
        return cls(base_url=url, api_key=key)


__all__ = ["LocalEndpointProvider"]
