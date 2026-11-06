"""Read-only discovery for a Codex sprint started outside Mission Control.

Mission Control must never claim, stop, or restart an observed external PID.
The canonical repository remains the authoritative Git Graph/acceptance source;
uncommitted work in a standalone clone is reported separately.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .observability import windows_process_snapshot


def _normalized(path: str | Path) -> str:
    value = str(path).replace("\\", "/")
    if value.startswith("//?/"):
        value = value[4:]
    return value.rstrip("/").casefold()


def _git(cwd: Path, *arguments: str, timeout: float = 7) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            capture_output=True, text=True, timeout=timeout, check=False,
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


_SESSION_FILE_LIMIT = 160
_SESSION_BYTES_LIMIT = 256 * 1024
_SESSION_HEADER_BYTES = 64 * 1024
_WORKSPACE_LIMIT = 12
_CHANGED_FILES_LIMIT = 30
_WORKSPACE_STATUS_BUDGET_SECONDS = 6.0
_METADATA_EVENT = re.compile(
    rb'^\s*\{[^{}]{0,4096}?"type"\s*:\s*"(?:session_meta|turn_context|event_msg)"'
)


def _registered_workspaces(repo: Path) -> list[Path]:
    """Return the canonical checkout and paths Git itself registers as worktrees."""
    output = _git(repo, "worktree", "list", "--porcelain", timeout=2)
    if output is None:
        return [repo]
    paths = [repo]
    for line in output.splitlines():
        if line.startswith("worktree "):
            candidate = Path(line[len("worktree "):])
            if candidate.exists() and _normalized(candidate) not in {
                _normalized(item) for item in paths
            }:
                paths.append(candidate)
    return paths


def _recent_rollouts(session_root: Path, now: datetime) -> tuple[list[Path], bool, bool]:
    """Select recent logs across a bounded date horizon by file metadata."""
    if not session_root.is_dir():
        return [], False, False
    cutoff = now - timedelta(hours=24)
    files: list[tuple[float, Path]] = []
    first_day = now.date() - timedelta(days=89)
    month_roots = {
        (day.year, day.month)
        for day_offset in range(90)
        if (day := now.date() - timedelta(days=day_offset))
    }
    day_roots: list[Path] = []
    for year, month in month_roots:
        month_root = session_root / f"{year:04}" / f"{month:02}"
        try:
            for day_root in month_root.iterdir():
                if day_root.is_dir() and day_root.name.isdigit() and len(day_root.name) == 2:
                    try:
                        day = datetime(year, month, int(day_root.name)).date()
                    except ValueError:
                        continue
                    if first_day <= day <= now.date():
                        day_roots.append(day_root)
        except OSError:
            continue
    for day_root in day_roots:
        try:
            entries = day_root.glob("rollout-*.jsonl")
            for path in entries:
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                if modified >= cutoff.timestamp() and modified <= now.timestamp() + 300:
                    files.append((modified, path))
        except OSError:
            continue
    files.sort(key=lambda item: item[0], reverse=True)
    truncated = len(files) > _SESSION_FILE_LIMIT
    return [path for _, path in files[:_SESSION_FILE_LIMIT]], truncated, True


def _metadata_session(path: Path, registered: dict[str, Path], now: datetime) -> dict[str, Any] | None:
    """Extract rollout metadata only; response items and message bodies are ignored."""
    identity: dict[str, Any] = {}
    model = effort = None
    usage: dict[str, int] | None = None
    latest_activity: datetime | None = None
    workspace: Path | None = None
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _SESSION_BYTES_LIMIT:
                head = handle.read(_SESSION_HEADER_BYTES)
                head = head[:head.rfind(b"\n") + 1]
                handle.seek(-(_SESSION_BYTES_LIMIT - _SESSION_HEADER_BYTES), 2)
                tail = handle.read(_SESSION_BYTES_LIMIT - _SESSION_HEADER_BYTES)
                first_newline = tail.find(b"\n")
                tail = tail[first_newline + 1:] if first_newline >= 0 else b""
                chunks = (head, tail)
            else:
                chunks = (handle.read(),)
        for chunk in chunks:
            for raw in chunk.splitlines():
                if not _METADATA_EVENT.match(raw):
                    continue
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError, UnicodeDecodeError):
                    continue
                payload = event.get("payload") or {}
                typ = event.get("type")
                if typ == "session_meta":
                    identity = payload if isinstance(payload, dict) else {}
                    cwd = identity.get("cwd")
                    workspace = registered.get(_normalized(cwd)) if cwd else None
                elif typ == "turn_context":
                    payload = payload if isinstance(payload, dict) else {}
                    cwd = payload.get("cwd")
                    # Current turn context is authoritative, even if session_meta
                    # names another origin. Only registered paths are accepted.
                    if cwd:
                        workspace = registered.get(_normalized(cwd))
                    model = payload.get("model") or model
                    effort = payload.get("effort") or payload.get("reasoning_effort") or effort
                elif typ == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    total = info.get("total_token_usage") if isinstance(info, dict) else None
                    if isinstance(total, dict):
                        parsed: dict[str, int] = {}
                        for source, target in (("input_tokens", "input_tokens"),
                                               ("cached_input_tokens", "cached_tokens"),
                                               ("output_tokens", "output_tokens")):
                            value = total.get(source)
                            if isinstance(value, int) and value >= 0:
                                parsed[target] = value
                        if parsed:
                            usage = parsed
                timestamp = event.get("timestamp")
                if isinstance(timestamp, str):
                    try:
                        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        if parsed_timestamp.tzinfo is None:
                            parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
                        parsed_timestamp = parsed_timestamp.astimezone(timezone.utc)
                        latest_activity = max(latest_activity, parsed_timestamp) if latest_activity else parsed_timestamp
                    except ValueError:
                        pass
    except (OSError, UnicodeError):
        return None
    if not identity or workspace is None:
        return None
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    if modified < now - timedelta(hours=24):
        return None
    latest_activity = max(latest_activity, modified) if latest_activity else modified
    source = identity.get("source")
    source = source if isinstance(source, dict) else {}
    spawn = ((source.get("subagent") or {}).get("thread_spawn") or {})
    counters = {
        "input_tokens": usage.get("input_tokens") if usage else None,
        "cached_tokens": usage.get("cached_tokens") if usage else None,
        "output_tokens": usage.get("output_tokens") if usage else None,
    }
    return {
        "session_id": identity.get("id"),
        "workspace": str(workspace),
        "role": "subagent" if spawn.get("parent_thread_id") else "root",
        "name": str(spawn.get("agent_nickname") or "Root agent")[:48],
        "parent_session_id": spawn.get("parent_thread_id"),
        "model": model or "unknown",
        "effort": effort or "unknown",
        "input_tokens": counters["input_tokens"],
        "cached_tokens": counters["cached_tokens"],
        "output_tokens": counters["output_tokens"],
        "latest_activity_utc": latest_activity.isoformat(),
        "log_modified_utc": modified.isoformat(),
    }


def _workspace_observation(
    workspace: Path, sessions: list[dict[str, Any]], *, timeout: float = 1.5,
) -> dict[str, Any]:
    status = _git(workspace, "status", "--porcelain=v2", "-z", "--branch",
                  "--untracked-files=all", timeout=timeout)
    changed_files: list[str] = []
    changed_count = None
    if status is not None:
        head = branch = None
        records = status.split("\0")
        changed_count = 0
        index = 0
        while index < len(records):
            record = records[index]
            while record.startswith("# "):
                line, separator, rest = record.partition("\n")
                if line.startswith("# branch.oid "):
                    head = line[len("# branch.oid "):]
                elif line.startswith("# branch.head "):
                    branch = line[len("# branch.head "):]
                if not separator:
                    record = ""
                    break
                record = rest
            if not record or record.startswith("# "):
                index += 1
                continue
            if record.startswith("? "):
                changed_count += 1
                changed_files.append(record[2:])
            elif record.startswith(("1 ", "2 ", "u ")):
                changed_count += 1
                max_split = 8 if record.startswith("1 ") else (9 if record.startswith("2 ") else 10)
                fields = record.split(" ", max_split)
                changed_files.append(fields[-1] if len(fields) > 1 else record)
                if record.startswith("2 "):
                    # Porcelain v2 -z emits the original rename path as its own record.
                    index += 1
            index += 1
    else:
        head = branch = None
    return {
        "workspace": str(workspace),
        "branch": branch,
        "head": head,
        "changed_count": changed_count,
        "changed_files": changed_files[:_CHANGED_FILES_LIMIT],
        "sessions": [item for item in sessions if _normalized(item["workspace"]) == _normalized(workspace)],
    }


def _process_workspaces(
    registered_paths: list[Path], processes: list[dict[str, Any]],
) -> tuple[set[str], set[int]]:
    matched: set[str] = set()
    pids: set[int] = set()
    for process in processes:
        if str(process.get("Name") or "").casefold() != "codex.exe":
            continue
        command = str(process.get("CommandLine") or "")
        try:
            tokens = shlex.split(command, posix=False)
        except ValueError:
            tokens = re.findall(r'"[^"]*"|\S+', command)
        process_matches = False
        index = 0
        while index < len(tokens):
            option = tokens[index].strip('"\'')
            value = None
            if option in ("-C", "--cd", "--cwd") and index + 1 < len(tokens):
                index += 1
                value = tokens[index].strip('"\'')
            elif option.startswith(("--cd=", "--cwd=")):
                value = option.split("=", 1)[1].strip('"\'')
            if value:
                target = _normalized(value)
                if target in {_normalized(path) for path in registered_paths}:
                    matched.add(target)
                    process_matches = True
            index += 1
        pid = process.get("ProcessId")
        if process_matches and isinstance(pid, int) and pid > 0:
            pids.add(pid)
    return matched, pids


def current_activity(
    repo: Path, *, session_root: Path | None = None,
    processes: list[dict[str, Any]] | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Observe current Codex activity in the canonical repo and registered worktrees."""
    repo = repo.resolve()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if session_root is None:
        session_root = Path.home() / ".codex" / "sessions"

    registered_paths = _registered_workspaces(repo)
    registered = {_normalized(path): path for path in registered_paths}
    if processes is None:
        processes = windows_process_snapshot()
    process_workspaces, pids = _process_workspaces(registered_paths, processes)
    rollout_paths, files_truncated, date_window_limited = _recent_rollouts(session_root, now)
    sessions = [item for path in rollout_paths
                if (item := _metadata_session(path, registered, now)) is not None]
    sessions.sort(key=lambda item: item["latest_activity_utc"], reverse=True)
    active_workspace_paths = [repo]
    for item in sessions:
        candidate = registered.get(_normalized(item["workspace"]))
        if candidate and _normalized(candidate) not in {_normalized(path) for path in active_workspace_paths}:
            active_workspace_paths.append(candidate)
    for candidate in registered_paths:
        if _normalized(candidate) in process_workspaces and _normalized(candidate) not in {
            _normalized(path) for path in active_workspace_paths
        }:
            active_workspace_paths.append(candidate)
    coverage_truncated = len(active_workspace_paths) > _WORKSPACE_LIMIT
    status_deadline = time.monotonic() + _WORKSPACE_STATUS_BUDGET_SECONDS
    workspaces = []
    for path in active_workspace_paths[:_WORKSPACE_LIMIT]:
        remaining = status_deadline - time.monotonic()
        workspaces.append(_workspace_observation(path, sessions, timeout=max(0.001, min(1.5, remaining))))
    status_unknown = any(item["changed_count"] is None for item in workspaces)
    observation = "RUNNING" if pids else ("RECENT_ACTIVITY" if sessions else "NO_RECENT_ACTIVITY")
    latest = sessions[0] if sessions else None
    usage = {}
    for key in ("input_tokens", "cached_tokens", "output_tokens"):
        values = [item[key] for item in sessions if item[key] is not None]
        usage[key] = sum(values) if len(values) == len(sessions) and values else None
    return {
        "available": True,
        "read_only": True,
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        "observation": observation,
        "pids": sorted(pids),
        "workspaces": workspaces,
        "sessions": sessions,
        "latest_activity_utc": latest["latest_activity_utc"] if latest else None,
        "latest_workspace": latest["workspace"] if latest else None,
        "tokens": usage,
        "coverage": {
            "partial": files_truncated or coverage_truncated or status_unknown or date_window_limited,
            "truncated": files_truncated or coverage_truncated,
            "notes": [note for note, enabled in (
                ("Recent session log limit reached", files_truncated),
                ("Session files are limited to the last 90 UTC date folders", date_window_limited),
                ("Recent workspace observation limit reached", coverage_truncated),
                ("One or more workspace Git statuses are unavailable", status_unknown),
            ) if enabled],
        },
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
