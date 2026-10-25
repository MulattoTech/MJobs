"""Read the existing loopback quota monitor without starting another Codex process."""
from datetime import datetime, timezone
import math
import httpx


def normalize_fleet_quota(sample, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        sampled = datetime.fromisoformat(sample["sampledAt"].replace("Z", "+00:00"))
        age = (now - sampled).total_seconds()
        remaining = sample["selectedRemainingPercent"]
        valid = (sample.get("fresh") is True and sample.get("monitorOnline") is True
                 and isinstance(remaining, (int, float)) and not isinstance(remaining, bool)
                 and math.isfinite(remaining) and 0 <= remaining <= 100 and 0 <= age <= 25)
    except (TypeError, ValueError, KeyError, AttributeError):
        valid = False
    if not valid:
        return {"available": False, "remaining_percent": None,
                "error": "Fleet Control quota is unavailable or stale",
                "source": "Fleet Control /api/quota"}
    raw_windows = sample.get("windows", [])
    windows = [w for w in raw_windows if isinstance(w, dict)] if isinstance(raw_windows, list) else []
    window = next((w for w in windows if w.get("label") == sample.get("monitoredWindow")), {})
    duration = window.get("windowDurationMins", window.get("windowMinutes"))
    if duration is None:
        duration = {"Weekly": 10080, "Five-hour": 300}.get(window.get("label"))
    reset = window.get("resetsAt")
    if isinstance(reset, str):
        try:
            reset = int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp())
        except ValueError:
            reset = None
    return {"available": True, "used_percent": 100 - remaining,
            "remaining_percent": remaining, "window_minutes": duration,
            "resets_at": reset,
            "ordinary_usage_allowed": sample.get("ordinaryUsageAllowed"),
            "sampled_at": sample["sampledAt"], "age_seconds": age,
            "source": "Fleet Control /api/quota", "windows": windows}


def read_fleet_quota():
    try:
        with httpx.Client(trust_env=False, timeout=.6, follow_redirects=False) as client:
            response = client.get("http://127.0.0.1:8787/api/quota")
            response.raise_for_status()
            return normalize_fleet_quota(response.json())
    except (httpx.HTTPError, ValueError):
        return None
