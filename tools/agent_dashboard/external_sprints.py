"""Read-only discovery for a Codex sprint started outside Mission Control.

Mission Control must never claim, stop, or restart an observed external PID.
The canonical repository remains the authoritative Git Graph/acceptance source;
uncommitted work in a standalone clone is reported separately.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .observability import windows_process_snapshot


def _normalized(path: str | Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()


def _git(cwd: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            capture_output=True, text=True, timeout=7, check=False,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _session_details(path: Path, workspace: Path) -> dict[str, Any] | None:
    """Read only session metadata/aggregate counters; no private conversation text."""
    identity: dict[str, Any] = {}
    model = effort = None
    usage: dict[str, Any] = {}
    spawned = 0
    try:
        with path.open(encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                payload = event.get("payload") or {}
                typ = event.get("type")
                if typ == "session_meta":
                    if _normalized(payload.get("cwd") or "") != _normalized(workspace):
                        return None
                    identity = payload
                elif typ == "turn_context":
                    model = payload.get("model") or model
                    effort = payload.get("effort") or payload.get("reasoning_effort") or effort
                elif typ == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    usage = info.get("total_token_usage") or usage
                elif (typ == "response_item" and payload.get("type") == "function_call"
                      and payload.get("name") == "spawn_agent"):
                    spawned += 1
    except (OSError, UnicodeError):
        return None
    if not identity:
        return None
    source = identity.get("source")
    source = source if isinstance(source, dict) else {}
    spawn = ((source.get("subagent") or {}).get("thread_spawn") or {})
    parent = spawn.get("parent_thread_id")
    return {
        "session_id": identity.get("id"),
        "role": "subagent" if parent else "root",
        "name": str(spawn.get("agent_nickname") or "Root agent")[:48],
        "parent_session_id": parent,
        "model": model or "unknown",
        "effort": effort or "unknown",
        "spawned": spawned,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_tokens": int(usage.get("cached_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "log_modified_utc": datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
    }


def current_round3(
    repo: Path, *, session_root: Path | None = None,
    processes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Observe only. Never mutate the target checkout, registry or Codex state."""
    repo = repo.resolve()
    state_file = repo / ".git" / "mjobs-p0p1-round3" / "latest.json"
    if not state_file.is_file():
        return {"available": False, "reason": "No Round 3 campaign checkpoint"}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8-sig"))
        saved_path = data.get("worktree")
        if not isinstance(saved_path, str) or not saved_path:
            return {"available": False, "reason": "Campaign path unavailable"}
        clone = Path(saved_path).resolve(strict=True)
        allowed = (repo / ".mjobs-recovery" / "active").resolve()
        if not clone.is_relative_to(allowed) or clone == allowed or not (clone / ".git").is_dir():
            return {"available": False, "reason": "Campaign is outside the canonical archive"}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"available": False, "reason": "Campaign checkpoint is unavailable"}

    if processes is None:
        processes = windows_process_snapshot()
    active_pids = []
    target = _normalized(clone)
    for process in processes:
        if str(process.get("Name") or "").casefold() != "codex.exe":
            continue
        command = _normalized(str(process.get("CommandLine") or ""))
        if target in command and (pid := process.get("ProcessId")):
            active_pids.append(int(pid))

    status = _git(clone, "status", "--porcelain=v1", "--untracked-files=normal")
    head = _git(clone, "rev-parse", "HEAD")
    branch = _git(clone, "branch", "--show-current")
    changed_files = []
    if status is not None:
        for line in status.splitlines()[:100]:
            if len(line) > 3:
                changed_files.append(line[3:])
    if session_root is None:
        session_root = Path.home() / ".codex" / "sessions"
    rollouts: list[Path] = []
    if session_root.is_dir():
        for path in session_root.glob("*/*/*/rollout-*.jsonl"):
            try:
                if path.stat().st_mtime >= state_file.stat().st_mtime - (7 * 86400):
                    rollouts.append(path)
            except OSError:
                continue
        rollouts.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    sessions = []
    for path in rollouts[:160]:
        item = _session_details(path, clone)
        if item is not None:
            sessions.append(item)
    sessions.sort(key=lambda entry: (entry["role"] != "root", entry["name"]))
    root_sessions = [x for x in sessions if x["role"] == "root"]
    latest_root = max(root_sessions, key=lambda x: x["log_modified_utc"], default=None)
    usage = {
        "input_tokens": sum(x["input_tokens"] for x in sessions),
        "cached_tokens": sum(x["cached_tokens"] for x in sessions),
        "output_tokens": sum(x["output_tokens"] for x in sessions),
    }
    goal_file = clone / ".git" / "afk-dev" / "goal.json"
    goal = None
    if goal_file.is_file():
        try:
            content = json.loads(goal_file.read_text(encoding="utf-8-sig"))
            goal = str(content.get("statement") or content.get("goal") or "")[:240] or None
        except (OSError, ValueError, TypeError):
            pass
    campaign_status = str(data.get("status") or "UNKNOWN")
    observation = "RUNNING" if active_pids else (
        "INACTIVE" if campaign_status in ("RUNNING", "READY") else campaign_status)
    return {
        "available": True,
        "read_only": True,
        "campaign": str(data.get("campaign") or ""),
        "checkpoint_status": campaign_status,
        "observation": observation,
        "pids": sorted(set(active_pids)),
        "workspace": str(clone),
        "branch": branch or str(data.get("branch") or ""),
        "head": head,
        "changed_count": len(changed_files) if status is not None else None,
        "changed_files": changed_files[:30],
        "goal": goal,
        "root_session": latest_root,
        "sessions": sessions[:20],
        "subagent_count": sum(x["role"] == "subagent" for x in sessions),
        "tokens": usage,
        "note": "External Codex session. Mission Control observes but does not own or stop it.",
    }
