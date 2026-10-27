from __future__ import annotations

import importlib.util
import json
import os
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


def test_atomic_json_never_follows_predictable_temp_symlink(tmp_path):
    publish = tmp_path / "analytics.json"
    canary = tmp_path / "canary.txt"
    canary.write_text("DO NOT CHANGE", encoding="utf-8")
    predictable = publish.with_suffix(publish.suffix + ".tmp")
    try:
        predictable.symlink_to(canary)
        symlink_created = True
    except OSError as error:
        if os.name != "nt" or getattr(error, "winerror", None) != 1314:
            raise
        os.link(canary, predictable)
        symlink_created = False

    module.atomic_json(publish, {"safe": True})

    assert canary.read_text(encoding="utf-8") == "DO NOT CHANGE"
    assert predictable.is_symlink() if symlink_created else predictable.samefile(canary)
    assert json.loads(publish.read_text(encoding="utf-8")) == {"safe": True}
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "analytics.json", "analytics.json.tmp", "canary.txt",
    ]


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


def test_local_go_offsets_dst_and_monotonic_suffix_are_real_utc_instants():
    assert module.epoch("2026-10-26 19:41:35.9196868 -0500 CDT") == module.epoch(
        "2026-10-27T00:41:35.919686Z"
    )
    assert module.epoch("2026-12-01 19:00:00 -0600 CST m=+42.000") == module.epoch(
        "2026-12-02T01:00:00Z"
    )
    assert module.epoch("2026-10-27 14:00:00 +1400 +14") is None
    assert module.epoch("2026-10-27 14:00:00 +1400 LINT") == module.epoch(
        "2026-10-27T00:00:00Z"
    )
    assert module.epoch("2026-10-26 19:00:00 CDT") is None
    assert module.epoch("2026-10-26 19:00:00 -9900 INVALID") is None


def test_import_native_includes_offset_day_boundary_without_reading_transcripts(
    tmp_path,
):
    home = tmp_path / "home"
    database = home / ".ao" / "data" / "ao.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as native:
        native.executescript("""
        CREATE TABLE usage_bindings(id INTEGER PRIMARY KEY, session_id TEXT);
        CREATE TABLE usage_sources(id INTEGER PRIMARY KEY, native_session_id TEXT);
        CREATE TABLE model_usage_events(id INTEGER PRIMARY KEY,binding_id INTEGER,
          usage_source_id INTEGER,created_at TEXT,model_id TEXT,usage_measurement_kind TEXT,
          input_tokens INTEGER,cached_input_tokens INTEGER,output_tokens INTEGER,estimated_cost_nanos INTEGER);
        CREATE TABLE session_terminal_turns(id TEXT PRIMARY KEY,session_id TEXT,
          native_session_id TEXT,requested_at TEXT,responded_at TEXT,
          user_request TEXT,agent_response TEXT);
        CREATE TABLE conversation_turns(id TEXT PRIMARY KEY,handled_by_session_id TEXT,
          requested_at TEXT,started_at TEXT,completed_at TEXT,state TEXT);
        """)
        native.execute("INSERT INTO usage_bindings VALUES(1,'agent-1')")
        native.execute("INSERT INTO usage_sources VALUES(1,'private-native')")
        native.execute(
            "INSERT INTO model_usage_events VALUES(1,1,1,?,?,?,?,?,?,?)",
            (
                "2026-10-26 01:31:00 +0000 UTC",
                "model",
                "native_reported",
                50,
                30,
                9,
                None,
            ),
        )
        native.executemany(
            "INSERT INTO session_terminal_turns VALUES(?,?,?,?,?,?,?)",
            [
                (
                    "valid",
                    "agent-1",
                    "private-native",
                    "2026-10-25 20:30:00 -0500 CDT",
                    "2026-10-25 20:32:00 -0500 CDT",
                    "DO_NOT_READ_PROMPT",
                    "DO_NOT_READ_RESPONSE",
                ),
                (
                    "old",
                    "agent-1",
                    "private-native",
                    "2026-10-26 13:00:00 +1400 LINT",
                    "2026-10-26 13:01:00 +1400 LINT",
                    "",
                    "",
                ),
                (
                    "recent",
                    "agent-1",
                    "private-native",
                    "2026-10-26 19:41:00 -0500 CDT",
                    "2026-10-26 19:43:00 -0500 CDT",
                    "",
                    "",
                ),
            ],
        )
    now = module.epoch("2026-10-27T01:00:00Z")
    cache = sqlite3.connect(":memory:")
    cache.row_factory = sqlite3.Row
    module.initialize(cache)
    turns, source = module.import_native(cache, ["agent-1"], now, home)
    assert source["turnWindowComplete"] is True
    assert source["unreadableTurnRecords"] == 0
    assert {turn["id"] for turn in turns} == {"terminal-valid", "terminal-recent"}
    assert all(turn["end"] - turn["start"] == 120 for turn in turns)
    assert "DO_NOT_READ" not in json.dumps(turns)
    assert cache.execute("SELECT SUM(output) FROM usage").fetchone()[0] == 9
    with sqlite3.connect(database) as native:
        native.execute(
            "INSERT INTO session_terminal_turns VALUES('bad','agent-1','private-native','ZZZ-invalid',NULL,'','')"
        )
    _, source = module.import_native(cache, ["agent-1"], now, home)
    assert source["turnWindowComplete"] is False
    assert source["unreadableTurnRecords"] == 1
    source_stat = database.stat()
    expected_identity = ":".join(
        (str(database.resolve()), str(source_stat.st_dev), str(source_stat.st_ino))
    )
    assert cache.execute(
        "SELECT v FROM meta WHERE k='source_identity'"
    ).fetchone()[0] == expected_identity
    cache.close()
