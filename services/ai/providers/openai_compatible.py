"""Bounded, safe OpenAI-compatible AI provider for standard chat completion endpoints."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Callable, Mapping

import httpx

from services.ai.providers import AIProviderError, ProviderCapabilities
from services.ai.telemetry import get_default_telemetry_engine
from services.ai.providers.endpoint_security import secure_base_url
from services.ai.providers.transport import _shutdown_response_socket, request_guard


DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
MAX_PROMPT_CHARACTERS = 200_000
MAX_SCHEMA_BYTES = 256_000
MAX_REQUEST_BYTES = 512_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_TIMEOUT_SECONDS = 300
_TASK = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def _sanitize_base_url(value: object) -> str:
    """Require HTTPS for remote providers; allow literal loopback HTTP."""
    return secure_base_url(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


class OpenAICompatibleProvider:
    """Safe, bounded provider for any standard OpenAI-compatible completions API."""

    name = "openai-compatible"
    capabilities = ProviderCapabilities(text_generation=True, structured_output=True, streaming=False)

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self.base_url = _sanitize_base_url(base_url)
        self.endpoint = f"{self.base_url}/chat/completions"
        self._api_key = api_key if isinstance(api_key, str) and api_key.strip() else None
        self._client_factory = client_factory

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleProvider":
        url = os.environ.get("MJOBS_OPENAI_COMPATIBLE_BASE_URL", DEFAULT_BASE_URL)
        key = os.environ.get("MJOBS_OPENAI_COMPATIBLE_API_KEY")
        return cls(base_url=url, api_key=key)

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

        payload: dict[str, object] = {
            "model": model,
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
            headers["Authorization"] = f"Bearer {self._api_key}"

        start_time = time.monotonic()
        client = None

        try:
            client = self._client_factory(
                trust_env=False,
                timeout=httpx.Timeout(float(timeout_seconds)),
            )
            with client, request_guard(client, deadline=start_time + float(timeout_seconds),
                                       cancellation_event=cancellation_event) as guard:
                with client.stream("POST", self.endpoint, json=payload, headers=headers, follow_redirects=False,
                                   extensions={"trace": guard.trace}) as response:
                    guard.bind_response(response)
                    if cancellation_event is not None and cancellation_event.is_set():
                        _shutdown_response_socket(response)
                        raise AIProviderError("AI_PROVIDER_CANCELLED")
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

            # Extract token usage telemetry safely if provided
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
                pass  # Telemetry failure should never break inference

            return content.strip()
        except Exception:
            raise AIProviderError("AI_PROVIDER_RESPONSE_INVALID") from None


__all__ = ["OpenAICompatibleProvider"]
