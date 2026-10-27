"""Read-only native fleet telemetry; publishes allowlisted statistics, never transcripts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import urllib.request
from pathlib import Path

UTC = dt.UTC


def epoch(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        # Go native timestamps retain an explicit numeric offset and zone name.
        # Trust the numeric offset, never guess a DST abbreviation or host zone.
        value = value.split(" m=")[0]
        value = re.sub(
            r"\s+([+-]\d{2})(\d{2})\s+[A-Za-z][A-Za-z0-9_+/-]*$", r"\1:\2", value
        )
        value = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=UTC).timestamp()
            if parsed.tzinfo is None
            else parsed.timestamp()
        )
    except (ValueError, OverflowError):
        return None


def iso(value):
    return (
        dt.datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
        if value is not None
        else None
    )


def number(value):
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def clean(value, limit=180):
    return "".join(c for c in str(value or "") if ord(c) >= 32)[:limit]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Local telemetry redirected")


def get(route):
    assert route in ("state", "sustain", "master", "quota")
    with urllib.request.build_opener(NoRedirect).open(
        "http://127.0.0.1:8787/api/" + route, timeout=8
    ) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Telemetry response too large")
        return json.loads(raw)


def atomic_json(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = None
    temp = None
    try:
        descriptor, raw_temp = tempfile.mkstemp(
            dir=file.parent, prefix=f".{file.name}.", suffix=".tmp"
        )
        temp = Path(raw_temp)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
        os.replace(temp, file)
        temp = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temp is not None:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def initialize(cache):
    cache.executescript("""PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY,agent TEXT,at REAL,native TEXT,model TEXT,kind TEXT,input INTEGER,cached INTEGER,output INTEGER,cost INTEGER);
    CREATE INDEX IF NOT EXISTS usage_agent_time ON usage(agent,at);
    CREATE TABLE IF NOT EXISTS transitions(id INTEGER PRIMARY KEY,agent TEXT,at REAL,state TEXT);
    CREATE INDEX IF NOT EXISTS transitions_agent_time ON transitions(agent,at);
    """)


def import_native(cache, ids, now, home):
    path = home / ".ao/data/ao.db"
    source_stat = path.stat()
    identity = ":".join(
        (str(path.resolve()), str(source_stat.st_dev), str(source_stat.st_ino))
    )
    old = cache.execute("SELECT v FROM meta WHERE k='source_identity'").fetchone()
    if old and old[0] != identity:
        raise ValueError(
            "Native database identity changed; historical telemetry needs reconciliation"
        )
    cache.execute("INSERT OR IGNORE INTO meta VALUES('source_identity',?)", (identity,))
    cursor = cache.execute("SELECT v FROM meta WHERE k='cursor'").fetchone()
    cursor = int(cursor[0]) if cursor else 0
    known = cache.execute("SELECT v FROM meta WHERE k='agents'").fetchone()
    known = set(json.loads(known[0])) if known else set()
    fresh = [x for x in ids if x not in known]
    # SQLite stores mixed-offset wall-clock strings. Widen the indexed prefilter
    # by the maximum civil UTC offset; apply the actual UTC window in Python.
    cutoff = dt.datetime.fromtimestamp(now - 86400 - 14 * 3600, UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    window_start = now - 86400
    deadline = time.monotonic() + 10
    slots = ",".join("?" for _ in ids)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as native:
        native.row_factory = sqlite3.Row
        native.execute("PRAGMA query_only=ON")
        native.set_progress_handler(lambda: int(time.monotonic() > deadline), 50000)
        high = native.execute(
            "SELECT COALESCE(MAX(id),0) FROM model_usage_events"
        ).fetchone()[0]
        query = f"""SELECT e.id,b.session_id,e.created_at,s.native_session_id,e.model_id,e.usage_measurement_kind,e.input_tokens,e.cached_input_tokens,e.output_tokens,e.estimated_cost_nanos
          FROM model_usage_events e JOIN usage_bindings b ON b.id=e.binding_id LEFT JOIN usage_sources s ON s.id=e.usage_source_id
          WHERE b.session_id IN ({slots}) AND e.id<=? AND e.created_at>=? AND (e.id>? {"OR b.session_id IN (" + ",".join("?" for _ in fresh) + ")" if fresh else ""}) ORDER BY e.id LIMIT 50001"""
        values = [*ids, high, cutoff, cursor, *fresh]
        usage = list(native.execute(query, values))
        complete = len(usage) <= 50000
        usage = usage[:50000]
        for row in usage:
            stamp = epoch(row["created_at"])
            if stamp is None or stamp > now + 10:
                continue
            cache.execute(
                "INSERT OR IGNORE INTO usage VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["session_id"],
                    stamp,
                    row["native_session_id"],
                    clean(row["model_id"], 100),
                    clean(row["usage_measurement_kind"], 50),
                    number(row["input_tokens"]),
                    number(row["cached_input_tokens"]),
                    number(row["output_tokens"]),
                    number(row["estimated_cost_nanos"]),
                ),
            )
        next_cursor = (
            high
            if complete
            else max(cursor, max((x["id"] for x in usage), default=cursor))
        )
        cache.execute(
            "INSERT OR REPLACE INTO meta VALUES('cursor',?)", (str(next_cursor),)
        )
        if complete:
            cache.execute(
                "INSERT OR REPLACE INTO meta VALUES('agents',?)", (json.dumps(ids),)
            )
        turns = []
        terminal_count = 0
        invalid_turns = 0
        for row in native.execute(
            f"SELECT id,session_id,native_session_id,requested_at,responded_at FROM session_terminal_turns WHERE session_id IN ({slots}) AND requested_at>=? ORDER BY id DESC LIMIT 10001",
            (*ids, cutoff),
        ):
            terminal_count += 1
            start, end = epoch(row["requested_at"]), epoch(row["responded_at"])
            if start is None or start > now + 10:
                invalid_turns += 1
                continue
            if row["responded_at"] is not None and (
                end is None or end < start or end > now + 10
            ):
                invalid_turns += 1
                end = None
            if start < window_start and (end is None or end < window_start):
                continue
            turns.append(
                {
                    "id": "terminal-" + str(row["id"]),
                    "agent": row["session_id"],
                    "native": row["native_session_id"],
                    "start": start,
                    "end": end if end is not None and end >= start else None,
                    "state": "completed"
                    if end is not None and end >= start
                    else "in_progress",
                    "interface": "terminal",
                }
            )
        terminal_complete = terminal_count < 10001
        chat_count = 0
        for row in native.execute(
            f"SELECT id,handled_by_session_id,requested_at,started_at,completed_at,state FROM conversation_turns WHERE handled_by_session_id IN ({slots}) AND requested_at>=? ORDER BY requested_at DESC LIMIT 1001",
            (*ids, cutoff),
        ):
            chat_count += 1
            start, end = (
                epoch(row["started_at"]) or epoch(row["requested_at"]),
                epoch(row["completed_at"]),
            )
            if start is None or start > now + 10:
                invalid_turns += 1
                continue
            if row["completed_at"] is not None and (
                end is None or end < start or end > now + 10
            ):
                invalid_turns += 1
                end = None
            if start < window_start and (end is None or end < window_start):
                continue
            turns.append(
                {
                    "id": "chat-" + str(row["id"]),
                    "agent": row["handled_by_session_id"],
                    "native": None,
                    "start": start,
                    "end": end if end is not None and end >= start else None,
                    "state": clean(row["state"], 40),
                    "interface": "chat",
                }
            )
    cache.execute("DELETE FROM usage WHERE at<?", (now - 7 * 86400,))
    cache.execute("DELETE FROM transitions WHERE at<?", (now - 7 * 86400,))
    cache.commit()
    return turns, {
        "available": True,
        "usageWindowComplete": complete,
        "turnWindowComplete": terminal_complete
        and chat_count < 1001
        and invalid_turns == 0,
        "unreadableTurnRecords": invalid_turns,
        "importedUsageEvents": len(usage),
        "observedAt": iso(now),
    }


def occupancy(turns, now, buckets=96):
    start = now - 86400
    spans = []
    for turn in sorted(turns, key=lambda x: x["start"]):
        if turn["end"] is None:
            continue
        a, b = (
            max(start, turn["start"]),
            min(now, turn["end"] if turn["end"] is not None else now),
        )
        if a >= b:
            continue
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(b, spans[-1][1]))
        else:
            spans.append((a, b))
    width = 86400 / buckets
    result = []
    for i in range(buckets):
        a, b = start + i * width, start + (i + 1) * width
        seconds = sum(max(0, min(b, y) - max(a, x)) for x, y in spans)
        result.append(round(min(1, seconds / width), 3))
    return result


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    return round(
        values[min(len(values) - 1, max(0, math.ceil(len(values) * p) - 1))], 2
    )


def assemble(cache, roster, sustain, master, quota, turns, source, now):
    all_usage = [
        dict(r)
        for r in cache.execute(
            "SELECT * FROM usage WHERE at>=? ORDER BY at", (now - 86400,)
        )
    ]
    agents = []
    state_counts = {}
    model_totals = {}
    token_series = [
        {"at": iso(now - 86400 + i * 900), "input": 0, "cached": 0, "output": 0}
        for i in range(96)
    ]
    for row in all_usage:
        bucket = min(95, max(0, int((row["at"] - (now - 86400)) / 900)))
        for key in ("input", "cached", "output"):
            token_series[bucket][key] += row[key] or 0
        model = model_totals.setdefault(
            row["model"],
            {"model": row["model"], "input": 0, "cached": 0, "output": 0, "events": 0},
        )
        model["events"] += 1
        for key in ("input", "cached", "output"):
            model[key] += row[key] or 0
    for s in roster["sessions"]:
        aid = s["id"]
        rows = [x for x in all_usage if x["agent"] == aid]
        history = sorted(
            (t for t in turns if t["agent"] == aid),
            key=lambda x: x["start"],
            reverse=True,
        )
        work = s.get("workState") or {
            "state": "executing" if s.get("busy") else "unknown",
            "label": "Executing" if s.get("busy") else "Awaiting workflow telemetry",
            "executing": bool(s.get("busy")),
        }
        if roster.get("offline"):
            work = {
                "state": "stopped",
                "label": "Offline / saved roster",
                "executing": False,
            }
        code = clean(work.get("state", "unknown"), 40)
        state_counts[code] = state_counts.get(code, 0) + 1
        last = cache.execute(
            "SELECT state FROM transitions WHERE agent=? ORDER BY at DESC LIMIT 1",
            (aid,),
        ).fetchone()
        if not last or last[0] != code:
            cache.execute(
                "INSERT INTO transitions(agent,at,state) VALUES(?,?,?)",
                (aid, now, code),
            )
        durations = [
            min(86400, t["end"] - t["start"]) for t in history if t["end"] is not None
        ]
        details = []
        for t in history[:30]:
            matched = []
            if t["native"] and t["end"] is not None:
                for e in rows:
                    if (
                        e["native"] != t["native"]
                        or not t["start"] <= e["at"] <= t["end"]
                    ):
                        continue
                    owners = [
                        v
                        for v in history
                        if v["native"] == t["native"]
                        and v["end"] is not None
                        and v["start"] <= e["at"] <= v["end"]
                    ]
                    if len(owners) == 1:
                        matched.append(e)
            details.append(
                {
                    "id": t["id"],
                    "startedAt": iso(t["start"]),
                    "endedAt": iso(t["end"]),
                    "durationSeconds": round(t["end"] - t["start"], 2)
                    if t["end"] is not None
                    else None,
                    "state": t["state"],
                    "interface": t["interface"],
                    "usageAttribution": "unique_native_conversation_and_time_window"
                    if matched
                    else "unavailable",
                    "usageEvents": len(matched),
                    "inputTokens": sum(x["input"] or 0 for x in matched)
                    if matched
                    else None,
                    "cachedInputTokens": sum(x["cached"] or 0 for x in matched)
                    if matched
                    else None,
                    "outputTokens": sum(x["output"] or 0 for x in matched)
                    if matched
                    else None,
                    "observedModels": sorted({x["model"] for x in matched}),
                }
            )
        totals = {
            k: sum(x[k] or 0 for x in rows) if rows else None
            for k in ["input", "cached", "output"]
        }
        transitions = [
            {"at": iso(x["at"]), "state": x["state"]}
            for x in cache.execute(
                "SELECT at,state FROM transitions WHERE agent=? AND at>=? ORDER BY at",
                (aid, now - 86400),
            )
        ]
        agents.append(
            {
                "id": clean(aid, 128),
                "name": clean(s.get("name") or aid),
                "role": clean(s.get("role")),
                "kind": s.get("kind"),
                "configuredModel": clean(s.get("model"), 100),
                "observedModels": sorted({x["model"] for x in rows}),
                "harness": clean(s.get("harness"), 30),
                "activity": clean(s.get("activity"), 40),
                "work": {
                    k: work.get(k)
                    for k in [
                        "state",
                        "label",
                        "reason",
                        "executing",
                        "queuePosition",
                        "estimatedStartAt",
                        "etaKind",
                        "nextEvaluationAt",
                        "assignedTask",
                    ]
                },
                "usage": {
                    **totals,
                    "events": len(rows),
                    "lastReportedAt": iso(max((x["at"] for x in rows), default=None)),
                    "cacheFraction": round(totals["cached"] / totals["input"], 4)
                    if totals["input"]
                    and totals["cached"] is not None
                    and 0 <= totals["cached"] <= totals["input"]
                    else None,
                },
                "turnsCompleted": len(durations),
                "medianTurnSeconds": percentile(durations, 0.5),
                "p95TurnSeconds": percentile(durations, 0.95),
                "latestTurns": details,
                "occupancy": occupancy(history, now),
                "transitions": transitions,
                "prs": [
                    {
                        "number": p.get("number"),
                        "state": clean(p.get("state"), 30),
                        "ci": clean(p.get("ci"), 30),
                    }
                    for p in s.get("prs", [])[:10]
                ],
            }
        )
    cache.commit()
    durations = [
        x["durationSeconds"]
        for a in agents
        for x in a["latestTurns"]
        if x["durationSeconds"] is not None
    ]
    productive = sustain.get("productivity", {})
    tasks = productive.get("tasks", [])
    return {
        "schemaVersion": 1,
        "generatedAt": iso(now),
        "staleAfterSeconds": 90,
        "window": {
            "start": iso(now - 86400),
            "end": iso(now),
            "label": "Last 24 hours",
        },
        "source": source,
        "summary": {
            "agents": len(agents),
            "executing": state_counts.get("executing", 0),
            "queued": state_counts.get("queued", 0),
            "reserved": state_counts.get("reserve", 0),
            "states": state_counts,
            "inputTokens": sum(x["input"] or 0 for x in all_usage),
            "cachedInputTokens": sum(x["cached"] or 0 for x in all_usage),
            "outputTokens": sum(x["output"] or 0 for x in all_usage),
            "usageEvents": len(all_usage),
            "completedTurns": sum(a["turnsCompleted"] for a in agents),
            "medianRecentTurnSeconds": percentile(durations, 0.5),
            "p95RecentTurnSeconds": percentile(durations, 0.95),
        },
        "dispatch": {
            "enabled": sustain.get("enabled"),
            "targetWorking": sustain.get("config", {}).get("targetWorking"),
            "productiveWork": sustain.get("config", {}).get("productiveWork", False),
            "reserveWorkers": sustain.get("config", {}).get("reserveWorkers", 0),
            "nextTick": sustain.get("nextTick"),
            "blocked": clean((sustain.get("latest") or {}).get("blocked")),
            "lastSuccess": sustain.get("lastSuccess"),
        },
        "readiness": {
            k: master.get("currentReadiness", {}).get(k)
            for k in ["ready", "verified", "total", "inspectionPending", "at"]
        },
        "quota": {
            "fresh": quota.get("fresh"),
            "remainingPercent": quota.get("selectedRemainingPercent")
            if quota.get("fresh")
            else None,
            "thresholdPercent": quota.get("config", {}).get("thresholdPercent"),
            "armed": quota.get("config", {}).get("autoKillArmed"),
        },
        "resources": {
            k: roster.get("metrics", {}).get(k)
            for k in ["cpu", "ramTotal", "ramFree", "logicalCores"]
        },
        "productivity": {
            "enabled": sustain.get("config", {}).get("productiveWork", False),
            "catalogCount": productive.get("catalogCount", 0),
            "assigned": productive.get("assigned", 0),
            "artifactsReady": productive.get("artifactsReady", 0),
            "noNewGap": productive.get("noNewGap", 0),
            "tasks": tasks[-150:],
        },
        "agents": agents,
        "models": sorted(
            model_totals.values(), key=lambda x: x["input"] + x["output"], reverse=True
        ),
        "tokenSeries": token_series,
        "notes": [
            "Reserved capacity is not executing activity. Queued start times are estimates, not promises.",
            "Tokens measure resource use, not accepted productivity. Maintenance drafts still require review.",
            "Turn tokens are shown only for unique native-conversation/time-window matches; unavailable attribution is not zero.",
            "Historical occupancy uses completed native turn intervals. Current execution is shown separately.",
            "No prompts, responses, native conversation identifiers, credentials, terminal output or absolute private paths are published.",
        ],
    }


def collect(root, publish, home):
    now = time.time()
    roster = get("state")
    sustain = get("sustain")
    master = get("master")
    quota = get("quota")
    stamp = epoch(roster.get("timestamp"))
    if stamp is None or now - stamp > 30 or stamp > now + 10:
        raise ValueError("Stale live roster")
    directory = root / "telemetry"
    directory.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(directory / "analytics.sqlite", timeout=2) as cache:
        cache.row_factory = sqlite3.Row
        initialize(cache)
        turns, source = import_native(
            cache, [s["id"] for s in roster["sessions"]], now, home
        )
        result = assemble(cache, roster, sustain, master, quota, turns, source, now)
    atomic_json(directory / "analytics.json", result)
    atomic_json(publish / "analytics.json", result)
    return {
        "at": result["generatedAt"],
        "agents": len(result["agents"]),
        "turns": result["summary"]["completedTurns"],
        "events": result["summary"]["usageEvents"],
        "bytes": (directory / "analytics.json").stat().st_size,
        "modelRequests": 0,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--publish", type=Path, required=True)
    p.add_argument("--loop", action="store_true")
    a = p.parse_args()
    while True:
        start = time.monotonic()
        try:
            print(json.dumps(collect(a.root, a.publish, Path.home())), flush=True)
        except Exception as error:
            # Retain old data and let its timestamp expire, rather than publish a fresh success.
            print(
                json.dumps(
                    {
                        "at": iso(time.time()),
                        "ok": False,
                        "reason": clean(type(error).__name__ + ": " + str(error), 180),
                    }
                ),
                flush=True,
            )
            if not a.loop:
                raise
        if not a.loop:
            break
        time.sleep(max(5, 30 - (time.monotonic() - start)))


if __name__ == "__main__":
    main()
