"""Fixed-origin, bounded, non-inference provider credential checks."""
from __future__ import annotations

from collections.abc import Callable
import threading
import time

import httpx

from services.ai.providers import AIProviderError
from services.ai.providers.transport import request_guard


_PROBE_TIMEOUT_SECONDS = 8.0
_ENDPOINTS = {
    "openai": ("https://api.openai.com/v1/models", "authorization"),
    "anthropic": ("https://api.anthropic.com/v1/models", "x-api-key"),
}


def test_provider_connection(provider: str, secret: str,
                             client_factory: Callable[..., httpx.Client] = httpx.Client,
                             *, cancellation_event: threading.Event | None = None) -> dict[str, str | None]:
    """Check authentication with one fixed GET, discarding its unused body.

    The owned transport shares one monotonic deadline across DNS, connect, TLS
    and headers. The API supplies a cancellation event so request disconnects
    close the in-flight transport. No raw response is retained.
    """
    configured = _ENDPOINTS.get(provider)
    if configured is None:
        return {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_UNSUPPORTED"}
    endpoint, auth_header = configured
    headers = {auth_header: f"Bearer {secret}" if provider == "openai" else secret}
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    try:
        with client_factory(trust_env=False, timeout=httpx.Timeout(_PROBE_TIMEOUT_SECONDS),
                            follow_redirects=False) as client, request_guard(
                                client, deadline=deadline, cancellation_event=cancellation_event) as guard:
            with client.stream("GET", endpoint, headers=headers, follow_redirects=False,
                               extensions={"trace": guard.trace}) as response:
                guard.bind_response(response)
                status = response.status_code
                # Authentication depends only on status. Never download,
                # decompress, parse or log the untrusted models/error body.
        if 200 <= status < 300:
            return {"status": "CONNECTED", "code": None}
        if status in (401, 403):
            return {"status": "FAILED", "code": "CREDENTIAL_REJECTED"}
        return {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_UNAVAILABLE"}
    except AIProviderError as error:
        code = {"AI_PROVIDER_TIMEOUT": "CREDENTIAL_PROVIDER_TIMEOUT",
                "AI_PROVIDER_CANCELLED": "CREDENTIAL_PROVIDER_CANCELLED"}.get(
                    error.code, "CREDENTIAL_PROVIDER_UNAVAILABLE")
        return {"status": "FAILED", "code": code}
    except httpx.TimeoutException:
        return {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_TIMEOUT"}
    except Exception:
        return {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_UNAVAILABLE"}
