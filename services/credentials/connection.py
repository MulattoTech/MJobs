"""Fixed-origin, bounded, non-inference provider credential checks."""
from __future__ import annotations

from collections.abc import Callable

import httpx


_ENDPOINTS = {
    "openai": ("https://api.openai.com/v1/models", "authorization"),
    "anthropic": ("https://api.anthropic.com/v1/models", "x-api-key"),
}


def test_provider_connection(provider: str, secret: str,
                             client_factory: Callable[..., httpx.Client] = httpx.Client) -> dict[str, str | None]:
    """Check provider authentication with one fixed GET and return only a stable code."""
    configured = _ENDPOINTS.get(provider)
    if configured is None:
        return {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_UNSUPPORTED"}
    endpoint, auth_header = configured
    headers = {auth_header: f"Bearer {secret}" if provider == "openai" else secret}
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    try:
        with client_factory(trust_env=False, timeout=httpx.Timeout(8.0), follow_redirects=False) as client:
            response = client.get(endpoint, headers=headers)
        if 200 <= response.status_code < 300:
            return {"status": "CONNECTED", "code": None}
        if response.status_code in (401, 403):
            return {"status": "FAILED", "code": "CREDENTIAL_REJECTED"}
        return {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_UNAVAILABLE"}
    except Exception:
        return {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_UNAVAILABLE"}
