from datetime import datetime, timezone
from unittest.mock import patch
from tools.agent_dashboard.fleet_quota import normalize_fleet_quota
from tools.agent_dashboard import observability


def test_fleet_quota_preserves_zero_and_rejects_stale_or_invalid():
    now = datetime(2026, 10, 25, 1, tzinfo=timezone.utc)
    sample = {"fresh": True, "monitorOnline": True, "sampledAt": now.isoformat(),
              "selectedRemainingPercent": 0, "ordinaryUsageAllowed": False,
              "monitoredWindow": "Weekly",
              "windows": [{"label": "Weekly", "resetsAt": "2026-10-30T17:50:49Z"}]}
    result = normalize_fleet_quota(sample, now)
    assert result["available"] is True
    assert result["remaining_percent"] == 0
    assert result["window_minutes"] == 10080
    assert result["resets_at"] == 1793382649
    assert normalize_fleet_quota({**sample, "windows": None}, now)["remaining_percent"] == 0
    for change in ({"fresh": False}, {"monitorOnline": False},
                   {"selectedRemainingPercent": True}, {"selectedRemainingPercent": 101},
                   {"sampledAt": "2026-10-25T00:59:00Z"},
                   {"sampledAt": "2026-10-25T01:01:00Z"}):
        result = normalize_fleet_quota({**sample, **change}, now)
        assert result["available"] is False
        assert result["remaining_percent"] is None


def test_shared_feed_avoids_native_codex_launch():
    shared = {"available": True, "remaining_percent": 0}
    with patch("tools.agent_dashboard.fleet_quota.read_fleet_quota", return_value=shared), \
         patch.object(observability, "_native_codex_exe", side_effect=AssertionError("No native process")):
        assert observability.read_codex_rate_limits(prefer_fleet=True) == shared


def test_background_command_uses_no_window_flag():
    with patch.object(observability.subprocess, "run") as run:
        observability._run(["fixture"])
        assert "creationflags" in run.call_args.kwargs
