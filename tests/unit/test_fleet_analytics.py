from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[2] / "tools/fleet_control/collect-analytics.py"
)
spec = importlib.util.spec_from_file_location("fleet_analytics", SOURCE)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_native_timestamp_formats_and_invalid_values():
    assert module.epoch("2026-10-26 23:24:00.636 +0000 UTC") == module.epoch(
        "2026-10-26T23:24:00.636Z"
    )
    assert module.epoch("not a date") is None
    assert module.epoch(None) is None
    assert module.number(True) is None
    assert module.number(float("nan")) is None


def test_historical_occupancy_uses_completed_union_not_open_waits():
    now = 100000.0
    turns = [
        {"start": now - 1800, "end": now - 900},
        {"start": now - 1200, "end": now - 600},
        {"start": now - 86400, "end": None},
    ]
    result = module.occupancy(turns, now)
    assert len(result) == 96
    assert sum(result[:-2]) == 0
    assert result[-2] == 1
    assert abs(result[-1] - 1 / 3) < 0.001
    assert max(result) <= 1


def test_export_has_no_prompts_native_ids_or_private_paths_and_missing_is_not_zero():
    now = 100000.0
    cache = sqlite3.connect(":memory:")
    cache.row_factory = sqlite3.Row
    module.initialize(cache)
    native = "PRIVATE_NATIVE_CONVERSATION"
    cache.execute(
        "INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            "agent-1",
            now - 10,
            native,
            "model-observed",
            "native_reported",
            20,
            10,
            5,
            300,
        ),
    )
    roster = {
        "sessions": [
            {
                "id": "agent-1",
                "name": "One",
                "role": "Implementation",
                "model": "configured",
                "kind": "worker",
                "harness": "codex",
                "activity": "idle",
                "busy": False,
                "prompt": "SECRET_PROMPT",
                "workspace": "C:\\private",
                "workState": {
                    "state": "reserve",
                    "label": "On-call reserve",
                    "executing": False,
                },
            }
        ],
        "metrics": {},
    }
    turns = [
        {
            "id": "turn-1",
            "agent": "agent-1",
            "native": native,
            "start": now - 20,
            "end": now - 5,
            "state": "completed",
            "interface": "terminal",
        },
        {
            "id": "turn-2",
            "agent": "agent-1",
            "native": native,
            "start": now - 100,
            "end": now - 80,
            "state": "completed",
            "interface": "terminal",
        },
    ]
    result = module.assemble(cache, roster, {}, {}, {}, turns, {"available": True}, now)
    serialized = json.dumps(result)
    assert all(x not in serialized for x in [native, "SECRET_PROMPT", "C:\\private"])
    assert result["summary"]["executing"] == 0
    assert result["summary"]["reserved"] == 1
    latest = result["agents"][0]["latestTurns"]
    assert latest[0]["inputTokens"] == 20
    assert latest[0]["usageAttribution"] == "unique_native_conversation_and_time_window"
    assert latest[1]["inputTokens"] is None
    assert result["agents"][0]["observedModels"] == ["model-observed"]
    assert result["agents"][0]["configuredModel"] == "configured"
    turns.append({**turns[0], "id": "overlapping-turn"})
    result = module.assemble(cache, roster, {}, {}, {}, turns, {"available": True}, now)
    assert result["agents"][0]["latestTurns"][0]["inputTokens"] is None
    cache.close()
