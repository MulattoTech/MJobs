"""Loopback-only Ollama provider with bounded, sanitized HTTP behavior."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from typing import Callable, Mapping
from urllib.parse import urlsplit

import httpx

from services.ai.providers import AIProviderError, ProviderCapabilities
from services.ai.providers.transport import _shutdown_response_socket, response_deadline


DEFAULT_BASE_URL = "http://127.0.0.1:11434"
MAX_PROMPT_CHARACTERS = 200_000
MAX_SCHEMA_BYTES = 256_000
MAX_REQUEST_BYTES = 512_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_TIMEOUT_SECONDS = 300
_TASK = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def _base_origin(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        address = ipaddress.ip_address(host) if host is not None else None
    except (ValueError, TypeError):
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID") from None
    if (parsed.scheme != "http" or address is None or not address.is_loopback
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")
    if port is not None and not 1 <= port <= 65535:
        raise AIProviderError("AI_PROVIDER_CONFIGURATION_INVALID")
    return f"http://{parsed.netloc}"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


class OllamaProvider:
    """Use only an explicitly loopback IP endpoint; never consult proxy env vars."""

    name = "ollama"
    capabilities = ProviderCapabilities(text_generation=True, structured_output=True)

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 client_factory: Callable[..., httpx.Client] = httpx.Client) -> None:
        self.base_url = _base_origin(base_url)
        self.endpoint = f"{self.base_url}/api/generate"
        self._client_factory = client_factory

    @classmethod
    def from_environment(cls) -> "OllamaProvider":
        return cls(os.environ.get("MJOBS_OLLAMA_BASE_URL", DEFAULT_BASE_URL))

    def generate(self, *, task: str, model: str | None = None, prompt: str,
                 timeout_seconds: float,
                 response_schema: Mapping[str, object] | None = None,
                 cancellation_event: threading.Event | None = None) -> str:
        if (not isinstance(task, str) or not _TASK.fullmatch(task)
                or not isinstance(model, str) or not _MODEL.fullmatch(model)
                or not isinstance(prompt, str) or not prompt.strip()
                or len(prompt) > MAX_PROMPT_CHARACTERS
                or type(timeout_seconds) not in (int, float)
                or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
            raise AIProviderError("AI_PROVIDER_REQUEST_INVALID")

        payload: dict[str, object] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        if response_schema is not None:
            if not isinstance(response_schema, Mapping):
                raise AIProviderError("AI_PROVIDER_REQUEST_INVALID")
            try:
                schema = dict(response_schema)
                encoded_schema = json.dumps(
                    schema, ensure_ascii=False, allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                raise AIProviderError("AI_PROVIDER_REQUEST_INVALID") from None
            if len(encoded_schema) > MAX_SCHEMA_BYTES:
                raise AIProviderError("AI_PROVIDER_REQUEST_INVALID")
            payload["format"] = schema

        try:
            encoded_request = json.dumps(
                payload, ensure_ascii=False, allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise AIProviderError("AI_PROVIDER_REQUEST_INVALID") from None
        if len(encoded_request) > MAX_REQUEST_BYTES:
            raise AIProviderError("AI_PROVIDER_REQUEST_INVALID")

        if cancellation_event is not None and cancellation_event.is_set():
            raise AIProviderError("AI_PROVIDER_CANCELLED")

        start_time = time.monotonic()
        client = None
        active_response = None
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
                                _shutdown_response_socket(active_response)
                                client.close()
                            except Exception:
                                pass
                            return

                watcher = threading.Thread(
                    target=close_client_when_cancelled,
                    name="mjobs-ollama-cancel-watch",
                    daemon=True,
                )
                watcher.start()
            with client:
                with client.stream("POST", self.endpoint, json=payload) as response, response_deadline(
                        response, deadline=start_time + float(timeout_seconds)):
                    active_response = response
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
        finally:
            stop_cancellation_watch.set()
            if watcher is not None:
                watcher.join(timeout=1)

        if cancellation_event is not None and cancellation_event.is_set():
            raise AIProviderError("AI_PROVIDER_CANCELLED")

        try:
            envelope = json.loads(
                body.decode("utf-8"), object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if (not isinstance(envelope, dict)
                    or type(envelope.get("done")) is not bool
                    or envelope["done"] is not True
                    or not isinstance(envelope.get("response"), str)
                    or not envelope["response"].strip()):
                raise ValueError("invalid provider response")
            return envelope["response"]
        except Exception:
            raise AIProviderError("AI_PROVIDER_RESPONSE_INVALID") from None


__all__ = ["OllamaProvider"]
