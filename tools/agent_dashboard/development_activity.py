"""Read only public MJobs delivery counts from the already-running Fleet Hub.

This is not Codex session telemetry or canonical requirement acceptance. A missing,
malformed, or untrusted response never becomes zero completed work.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import httpx


_HUB_DELIVERY_URL = "http://127.0.0.1:4320/api/delivery"
_UNAVAILABLE = {
    "available": False,
    "reason": "Fleet Overview development activity is unavailable; accepted requirements are unchanged.",
}


def read_development_activity(transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Bounded, no-redirect loopback read. No client-provided URL or token."""
    try:
        with httpx.Client(
            transport=transport, timeout=httpx.Timeout(3.0, connect=1.0),
            trust_env=False, follow_redirects=False,
        ) as client:
            response = client.get(_HUB_DELIVERY_URL, headers={"Accept": "application/json"})
            response.raise_for_status()
            if len(response.content) > 8192:
                return dict(_UNAVAILABLE)
            data = response.json()

        if not isinstance(data, dict) or data.get("available") is not True:
            return dict(_UNAVAILABLE)
        if data.get("source") != "GitHub public MJobs PR API":
            return dict(_UNAVAILABLE)
        counts = {key: data.get(key) for key in ("openTotal", "openDrafts", "mergedSince")}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            return dict(_UNAVAILABLE)
        if counts["openDrafts"] > counts["openTotal"]:
            return dict(_UNAVAILABLE)
        since = data.get("sinceUTC")
        observed = data.get("observedAt")
        if not isinstance(since, str) or len(since) != 10:
            return dict(_UNAVAILABLE)
        if not isinstance(observed, str) or len(observed) > 40:
            return dict(_UNAVAILABLE)
        date.fromisoformat(since)
        timestamp = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return dict(_UNAVAILABLE)
    except (httpx.HTTPError, ValueError, TypeError, OSError):
        return dict(_UNAVAILABLE)

    return {
        "available": True, **counts,
        "sinceUTC": since, "observedAt": observed,
        "source": "Fleet Control Hub (read-only)",
        "note": "Open and merged PRs show development delivery, not accepted requirements or running agents.",
    }
