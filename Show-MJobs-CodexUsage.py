r"""Read-only Codex model/subagent/token accounting from persisted JSONL sessions.

Run from C:\Tools\Dev\MJobs:
    python Show-MJobs-CodexUsage.py --round 2
    python Show-MJobs-CodexUsage.py --round 3
Token processing is NOT account billing and does not expose quota percentages.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def inspect_session(path: Path, target: str):
    first = None
    model = "unknown"
    reasoning = "unknown"
    role = "primary"
    total = {}
    agent_calls = 0
    start = end = None
    try:
        stream = path.open(encoding="utf-8-sig", errors="replace")
        with stream:
            for line in stream:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                payload = obj.get("payload") or {}
                t = obj.get("type")
                if t == "session_meta":
                    first = payload
                    if str(payload.get("cwd", "")).casefold().replace("/", "\\") != target:
                        return None
                    src = payload.get("source")
                    if isinstance(src, dict) and "subagent" in src:
                        info = src["subagent"].get("thread_spawn") or {}
                        role = "subagent: " + str(info.get("agent_nickname") or info.get("agent_path") or "unnamed")
                if first is None:
                    continue
                when = parse_datetime(obj.get("timestamp"))
                if when:
                    start = min(start, when) if start else when
                    end = max(end, when) if end else when
                if t == "turn_context":
                    model = str(payload.get("model") or model)
                    reasoning = str(payload.get("effort") or payload.get("reasoning_effort") or reasoning)
                elif t == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    total = info.get("total_token_usage") or total
                elif t == "response_item" and payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                    agent_calls += 1
    except OSError:
        return None
    if not first:
        return None
    return dict(file=path.name, role=role, model=model, effort=reasoning,
                input=int(total.get("input_tokens", 0)),
                cached=int(total.get("cached_input_tokens", 0)),
                output=int(total.get("output_tokens", 0)),
                reasoning_output=int(total.get("reasoning_output_tokens", 0)),
                spawned=agent_calls, start=start, end=end)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=int, choices=(2, 3), default=2)
    args = parser.parse_args()
    state = ROOT / ".git" / f"mjobs-p0p1-round{args.round}" / "latest.json"
    if not state.is_file():
        raise SystemExit(f"No Round {args.round} checkpoint: {state}")
    meta = json.loads(state.read_text(encoding="utf-8-sig"))
    workspace = str(meta.get("worktree") or meta.get("workspace") or "").casefold().replace("/", "\\")
    if not workspace:
        raise SystemExit("Checkpoint has no workspace path")
    sessions_dir = Path.home() / ".codex" / "sessions"
    records = []
    for log in sessions_dir.glob("**/rollout-*.jsonl"):
        obj = inspect_session(log, workspace)
        if obj:
            records.append(obj)
    records.sort(key=lambda x: x["start"] or datetime.min.replace(tzinfo=timezone.utc))
    if not records:
        raise SystemExit("No matching Codex session records located; token/account quota unknown")
    print(f"MJobs Round {args.round} | campaign={meta.get('campaign')} | status={meta.get('status') or meta.get('state')}")
    print("Models and verified spawned agents:")
    for r in records:
        print(f"  {r['role']}: {r['model']} ({r['effort']}); "
              f"input={r['input']:,}, cached={r['cached']:,}, "
              f"output={r['output']:,}, reasoning={r['reasoning_output']:,}, agent spawns={r['spawned']}")
    inp = sum(x["input"] for x in records)
    cached = sum(x["cached"] for x in records)
    out = sum(x["output"] for x in records)
    first = min((x["start"] for x in records if x["start"]), default=None)
    last = max((x["end"] for x in records if x["end"]), default=None)
    minutes = max(0.01, (last - first).total_seconds() / 60) if first and last else None
    print(f"Combined processed input={inp:,}; cached={cached:,} ({100*cached/inp:.1f}% of input); "
          f"uncached input={inp-cached:,}; output={out:,}; combined processed={inp+out:,}")
    if minutes:
        print(f"Observed span ~{minutes:.1f} min; average processed tokens/min ~{(inp+out)/minutes:,.0f} "
              "(mostly cached, not API charges)")
    print("Account 5h/weekly usage %: unavailable in these session logs. "
          "Use Codex interactive /status for plan quota information.")


if __name__ == "__main__":
    main()
