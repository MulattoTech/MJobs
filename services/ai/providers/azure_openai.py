"""Azure OpenAI provider with deployment-aware routing, api-version handling, and telemetry."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Callable, Mapping
from urllib.parse import urlsplit

import httpx

from services.ai.providers import AIProviderError, ProviderCapabilities
from services.ai.telemetry import get_default_telemetry_engine


DEFAULT_AZURE_API_VERSION = "2024-02-15-preview"
DEFAULT_AZURE_BASE_URL = "https://example-resource.openai.azure.com"
MAX_PROMPT_CHARACTERS = 200_000
MAX_SCHEMA_BYTES = 256_000
MAX_REQUEST_BYTES = 512_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_TIMEOUT_SECONDS = 300
_TASK = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def _sanitize_azure_base_url(value: object) -> str:
    """Validate Azure OpenAI base endpoint, ensuring HTTPS or loopback HTTP."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID") from None

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")
    if parsed.username is not None or parsed.password is not None:
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")
    if parsed.query or parsed.fragment:
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")
    if port is not None and not 1 <= port <= 65535:
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")

    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


class AzureOpenAIProvider:
    """Safe, bounded provider for Azure OpenAI service deployments."""

    name = "azure-openai"
    capabilities = ProviderCapabilities(text_generation=True, structured_output=True, streaming=False)

    def __init__(
        self,
        base_url: str = DEFAULT_AZURE_BASE_URL,
        api_key: str | None = None,
        api_version: str = DEFAULT_AZURE_API_VERSION,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self.base_url = _sanitize_azure_base_url(base_url)
        self.api_version = api_version.strip() if isinstance(api_version, str) and api_version.strip() else DEFAULT_AZURE_API_VERSION
        self._api_key = api_key if isinstance(api_key, str) and api_key.strip() else None
        self._client_factory = client_factory

    @classmethod
    def from_environment(cls) -> "AzureOpenAIProvider":
        url = os.environ.get("AZURE_OPENAI_ENDPOINT") or os.environ.get("MJOBS_AZURE_OPENAI_BASE_URL") or DEFAULT_AZURE_BASE_URL
        key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("MJOBS_AZURE_OPENAI_API_KEY")
        version = os.environ.get("AZURE_OPENAI_API_VERSION") or DEFAULT_AZURE_API_VERSION
        return cls(base_url=url, api_key=key, api_version=version)

    def get_deployment_endpoint(self, deployment: str) -> str:
        """Construct deployment-specific chat completions endpoint with api-version query."""
        clean_deployment = deployment.strip()
        return f"{self.base_url}/openai/deployments/{clean_deployment}/chat/completions?api-version={self.api_version}"

    def generate(
        self,
        *,
        task: str,
        model: str | None = None,
        prompt: str,
        timeout_seconds: float,
        response_schema: Mapping[str, object] | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        if (
            not isinstance(task, str)
            or not _TASK.fullmatch(task)
            or not isinstance(model, str)
            or not _MODEL.fullmatch(model)
            or not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > MAX_PROMPT_CHARACTERS
            or type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise AIProviderError("AI_PROVIDER_REQUEST_INVALID")

        endpoint = self.get_deployment_endpoint(model)

        payload: dict[str, object] = {
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }

        if response_schema is not None:
            if not isinstance(response_schema, Mapping):
                raise AIProviderError("AI_PROVIDER_REQUEST_INVALID")
            try:
                schema = dict(response_schema)
                encoded_schema = json.dumps(
                    schema,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                raise AIProviderError("AI_PROVIDER_REQUEST_INVALID") from None
            if len(encoded_schema) > MAX_SCHEMA_BYTES:
                raise AIProviderError("AI_PROVIDER_REQUEST_INVALID")
            payload["response_format"] = {"type": "json_object"}

        try:
            encoded_request = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise AIProviderError("AI_PROVIDER_REQUEST_INVALID") from None

        if len(encoded_request) > MAX_REQUEST_BYTES:
            raise AIProviderError("AI_PROVIDER_REQUEST_INVALID")

        if cancellation_event is not None and cancellation_event.is_set():
            raise AIProviderError("AI_PROVIDER_CANCELLED")

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key

        start_time = time.monotonic()
        client = None
        stop_cancellation_watch = threading.Event()
        watcher = None

        try:
            client = self._client_factory(
                trust_env=False,
                timeout=httpx.Timeout(float(timeout_seconds)),
            )
            if cancellation_event is not None:
                def close_client_when_cancelled():
                    while not stop_cancellation_watch.wait(0.01):
                        if cancellation_event.is_set():
                            try:
                                client.close()
                            except Exception:
                                pass
                            return

                watcher = threading.Thread(
                    target=close_client_when_cancelled,
                    name=f"mjobs-{self.name}-cancel-watch",
                    daemon=True,
                )
                watcher.start()

            with client:
                with client.stream("POST", endpoint, json=payload, headers=headers) as response:
                    if not 200 <= response.status_code < 300:
                        raise AIProviderError("AI_PROVIDER_HTTP_ERROR")
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        if cancellation_event is not None and cancellation_event.is_set():
                            raise AIProviderError("AI_PROVIDER_CANCELLED")
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise AIProviderError("AI_PROVIDER_RESPONSE_TOO_LARGE")
                        body.extend(chunk)
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise AIProviderError("AI_PROVIDER_CANCELLED")
        except AIProviderError as error:
            if cancellation_event is not None and cancellation_event.is_set():
                raise AIProviderError("AI_PROVIDER_CANCELLED") from None
            raise error
        except httpx.TimeoutException:
            if cancellation_event is not None and cancellation_event.is_set():
                raise AIProviderError("AI_PROVIDER_CANCELLED") from None
            raise AIProviderError("AI_PROVIDER_TIMEOUT") from None
        except httpx.TransportError:
            if cancellation_event is not None and cancellation_event.is_set():
                raise AIProviderError("AI_PROVIDER_CANCELLED") from None
            raise AIProviderError("AI_PROVIDER_UNAVAILABLE") from None
        except Exception:
            if cancellation_event is not None and cancellation_event.is_set():
                raise AIProviderError("AI_PROVIDER_CANCELLED") from None
            raise AIProviderError("AI_PROVIDER_UNAVAILABLE") from None
        finally:
            stop_cancellation_watch.set()
            if watcher is not None:
                watcher.join(timeout=1)

        latency_ms = (time.monotonic() - start_time) * 1000

        if cancellation_event is not None and cancellation_event.is_set():
            raise AIProviderError("AI_PROVIDER_CANCELLED")

        try:
            envelope = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(envelope, dict):
                raise ValueError("response must be a JSON object")

            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) == 0:
                raise ValueError("missing choices in response")

            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                raise ValueError("choice must be an object")

            message = first_choice.get("message")
            if not isinstance(message, dict):
                raise ValueError("choice message must be an object")

            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("message content must be a non-empty string")

            usage = envelope.get("usage")
            prompt_tokens = 0
            completion_tokens = 0
            if isinstance(usage, dict):
                p_tok = usage.get("prompt_tokens")
                c_tok = usage.get("completion_tokens")
                if isinstance(p_tok, int) and p_tok >= 0:
                    prompt_tokens = p_tok
                if isinstance(c_tok, int) and c_tok >= 0:
                    completion_tokens = c_tok

            try:
                engine = get_default_telemetry_engine()
                engine.record_attempt(
                    task=task,
                    provider=self.name,
                    model=model,
                    latency_ms=latency_ms,
                    status="SUCCESS",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            except Exception:
                pass

            return content.strip()
        except Exception:
            raise AIProviderError("AI_PROVIDER_RESPONSE_INVALID") from None


__all__ = ["AzureOpenAIProvider"]
