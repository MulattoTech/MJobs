from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _run(args: list[str], *, timeout: int = 10, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def offload_layers(block_count: int, percent: int) -> int:
    percent = max(0, min(100, int(percent)))
    return max(0, min(block_count, round(block_count * percent / 100)))


def normalize_codex_rate_limits(payload: dict[str, Any]) -> dict[str, Any]:
    rate = payload.get("rateLimits") or {}
    primary = rate.get("primary") or {}
    secondary = rate.get("secondary") or {}
    used = primary.get("usedPercent")
    remaining = None if used is None else max(0, 100 - int(used))
    secondary_used = secondary.get("usedPercent")
    return {
        "available": used is not None,
        "used_percent": used,
        "remaining_percent": remaining,
        "window_minutes": primary.get("windowDurationMins"),
        "resets_at": primary.get("resetsAt"),
        "secondary_used_percent": secondary_used,
        "secondary_remaining_percent": None if secondary_used is None else max(0, 100 - int(secondary_used)),
        "secondary_window_minutes": secondary.get("windowDurationMins"),
        "secondary_resets_at": secondary.get("resetsAt"),
        "plan_type": rate.get("planType"),
        "ordinary_usage_allowed": payload.get("ordinaryUsageAllowed"),
        "credits_balance": (rate.get("credits") or {}).get("balance"),
        "has_credits": (rate.get("credits") or {}).get("hasCredits"),
        "credits_unlimited": (rate.get("credits") or {}).get("unlimited"),
        "reset_credits_available": (payload.get("rateLimitResetCredits") or {}).get("availableCount"),
        "spend_control_reached": rate.get("spendControlReached"),
        "rate_limit_reached_type": rate.get("rateLimitReachedType"),
        "limit_id": rate.get("limitId"),
    }


def _native_codex_exe() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    root = Path(appdata) / "npm" / "node_modules" / "@openai" / "codex" / "node_modules"
    candidates = list(root.glob("@openai/codex-win32-*/vendor/*/bin/codex.exe"))
    return candidates[0] if candidates else None


def read_codex_rate_limits(timeout: float = 12.0) -> dict[str, Any]:
    exe = _native_codex_exe()
    if not exe or not exe.exists():
        return {"available": False, "error": "native Codex executable not found"}
    process = subprocess.Popen(
        [str(exe), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    lines: queue.Queue[str] = queue.Queue()

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=reader, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    try:
        send({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "mjobs-agent-dashboard", "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            },
        })
        deadline = time.monotonic() + timeout / 2
        initialized = False
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.4)
            except queue.Empty:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 1 and "result" in message:
                initialized = True
                break
        if not initialized:
            return {"available": False, "error": "Codex app-server initialize timed out"}

        send({"id": 2, "method": "account/rateLimits/read"})
        deadline = time.monotonic() + timeout / 2
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.4)
            except queue.Empty:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2:
                if "result" in message:
                    return normalize_codex_rate_limits(message["result"])
                return {"available": False, "error": str(message.get("error") or "unknown Codex RPC error")}
        return {"available": False, "error": "Codex rate-limit read timed out"}
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    finally:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def log_activity(path: Path, lane: str) -> dict[str, Any]:
    if not path.exists():
        return {"last_activity_at": None, "last_activity_seconds": None, "session_id": None}
    stat = path.stat()
    age = max(0.0, time.time() - stat.st_mtime)
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - 2_000_000))
            tail = handle.read().decode("utf-8", errors="replace")
    except Exception:
        tail = ""
    session_id = None
    patterns = {
        "codex": [r"session id:\s*([0-9a-f-]{20,})"],
        "antigravity": [r"conversation(?: id)?[:=]\s*([0-9a-f-]{20,})"],
        "ollama": [],
    }
    for pattern in patterns.get(lane, []):
        matches = re.findall(pattern, tail, flags=re.I)
        if matches:
            session_id = matches[-1]
            break
    if lane == "antigravity" and not session_id:
        brain = Path.home() / ".gemini" / "antigravity-cli" / "brain"
        try:
            dirs = [p for p in brain.iterdir() if p.is_dir()]
            if dirs:
                latest = max(dirs, key=lambda p: p.stat().st_mtime)
                if re.fullmatch(r"[0-9a-f-]{20,}", latest.name, flags=re.I):
                    session_id = latest.name
        except Exception:
            pass
    return {
        "last_activity_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "last_activity_seconds": round(age, 1),
        "session_id": session_id,
    }


def windows_process_snapshot() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,WorkingSetSize,CreationDate,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    result = _run(["powershell.exe", "-NoProfile", "-Command", ps], timeout=8)
    if result.returncode or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
        return payload if isinstance(payload, list) else [payload]
    except json.JSONDecodeError:
        return []


def process_tree(snapshot: list[dict[str, Any]], root_pid: int | None) -> list[dict[str, Any]]:
    if not root_pid:
        return []
    by_parent: dict[int, list[dict[str, Any]]] = {}
    by_pid: dict[int, dict[str, Any]] = {}
    for row in snapshot:
        try:
            pid = int(row.get("ProcessId"))
            parent = int(row.get("ParentProcessId") or 0)
        except Exception:
            continue
        by_pid[pid] = row
        by_parent.setdefault(parent, []).append(row)
    todo = [int(root_pid)]
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    while todo:
        pid = todo.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        row = by_pid.get(pid)
        if row:
            found.append({
                "pid": pid,
                "parent_pid": int(row.get("ParentProcessId") or 0),
                "name": row.get("Name"),
                "working_set_mb": round(float(row.get("WorkingSetSize") or 0) / 1048576, 1),
                "command_line": row.get("CommandLine"),
            })
        for child in by_parent.get(pid, []):
            try:
                todo.append(int(child.get("ProcessId")))
            except Exception:
                pass
    return found


def windows_gpu_snapshot() -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False}
    script = r"""
$c=Get-Counter '\GPU Engine(*)\Utilization Percentage' -SampleInterval 1 -MaxSamples 1 -ErrorAction SilentlyContinue
if(-not $c){ '{}' ; exit }
$rows=@()
foreach($s in $c.CounterSamples){
  if($s.Path -match 'pid_(\d+)_'){
    $rows += [pscustomobject]@{pid=[int]$Matches[1]; value=[double]$s.CookedValue}
  }
}
$rows | ConvertTo-Json -Compress
"""
    result = _run(["powershell.exe", "-NoProfile", "-Command", script], timeout=6)
    if result.returncode or not result.stdout.strip():
        return {"available": False}
    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            rows = [rows] if rows else []
    except json.JSONDecodeError:
        rows = []
    by_pid: dict[int, float] = {}
    all_values: list[float] = []
    for row in rows:
        try:
            pid = int(row["pid"])
            value = max(0.0, float(row["value"]))
        except Exception:
            continue
        all_values.append(value)
        by_pid[pid] = max(by_pid.get(pid, 0.0), value)
    return {
        "available": True,
        "utilization_percent": round(max(all_values, default=0.0), 1),
        "by_pid": {str(pid): round(value, 1) for pid, value in by_pid.items() if value >= 0.1},
    }


def ollama_ps() -> dict[str, Any]:
    result = _run(["ollama", "ps"], timeout=5)
    if result.returncode:
        return {"available": False, "error": result.stderr.strip()}
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return {"available": True, "models": []}
    models = []
    for line in lines[1:]:
        parts = re.split(r"\s{2,}", line.strip())
        models.append({
            "name": parts[0] if parts else None,
            "id": parts[1] if len(parts) > 1 else None,
            "size": parts[2] if len(parts) > 2 else None,
            "processor": parts[3] if len(parts) > 3 else None,
            "context": parts[4] if len(parts) > 4 else None,
            "until": parts[5] if len(parts) > 5 else None,
            "raw": line.strip(),
        })
    return {"available": True, "models": models}


def _ensure_opencode_model(alias: str, display_name: str) -> None:
    path = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    if not path.exists():
        return
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        models = config.setdefault("provider", {}).setdefault("ollama", {}).setdefault("models", {})
        if alias not in models:
            models[alias] = {"name": display_name}
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
    except Exception:
        return


def prepare_ollama_model(base_model: str, percent: int, bus: Path) -> tuple[str, dict[str, Any]]:
    percent = max(0, min(100, int(percent)))
    raw = base_model.removeprefix("ollama/")
    if percent >= 100:
        return base_model, {"percent": 100, "layers": None, "block_count": None, "derived": False}

    show = _run(["ollama", "show", "--verbose", raw], timeout=20)
    if show.returncode:
        raise RuntimeError(show.stderr.strip() or f"Unable to inspect Ollama model {raw}")
    match = re.search(r"\.block_count\s+(\d+)", show.stdout)
    if not match:
        raise RuntimeError(f"Unable to determine transformer block count for {raw}")
    block_count = int(match.group(1))
    layers = offload_layers(block_count, percent)
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-").lower()
    alias = f"mjobs-{safe}-gpu{percent}"
    model_dir = bus / "ollama-models"
    model_dir.mkdir(parents=True, exist_ok=True)
    modelfile = model_dir / f"{alias}.Modelfile"
    content = f"FROM {raw}\nPARAMETER num_gpu {layers}\n"
    if not modelfile.exists() or modelfile.read_text(encoding="utf-8") != content:
        modelfile.write_text(content, encoding="utf-8")
    created = _run(["ollama", "create", alias, "-f", str(modelfile)], timeout=120)
    if created.returncode:
        raise RuntimeError(created.stderr.strip() or created.stdout.strip() or "ollama create failed")
    _ensure_opencode_model(alias, f"MJobs capped {raw} ({percent}% offload)")
    return f"ollama/{alias}", {
        "percent": percent,
        "layers": layers,
        "block_count": block_count,
        "derived": True,
        "alias": alias,
    }


def _token_accumulator() -> dict[str, float]:
    return {
        "assistant_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cumulative_total_tokens": 0,
        "cost": 0.0,
        "generation_wall_seconds": 0.0,
    }


def _add_message_usage(acc: dict[str, float], data: dict[str, Any]) -> None:
    if data.get("role") != "assistant":
        return
    tokens = data.get("tokens") or {}
    cache = tokens.get("cache") or {}
    acc["assistant_turns"] += 1
    acc["input_tokens"] += int(tokens.get("input") or 0)
    acc["output_tokens"] += int(tokens.get("output") or 0)
    acc["reasoning_tokens"] += int(tokens.get("reasoning") or 0)
    acc["cache_read_tokens"] += int(cache.get("read") or 0)
    acc["cache_write_tokens"] += int(cache.get("write") or 0)
    acc["cumulative_total_tokens"] += int(tokens.get("total") or 0)
    acc["cost"] += float(data.get("cost") or 0)
    timing = data.get("time") or {}
    created = timing.get("created")
    completed = timing.get("completed")
    if created and completed and completed >= created:
        acc["generation_wall_seconds"] += (completed - created) / 1000.0


def _finalize_token_accumulator(acc: dict[str, float]) -> dict[str, Any]:
    prompt_material = (
        acc["input_tokens"] + acc["cache_read_tokens"] + acc["cache_write_tokens"]
    )
    turns = int(acc["assistant_turns"])
    wall = float(acc["generation_wall_seconds"])
    output = int(acc["output_tokens"])
    return {
        **{k: int(v) for k, v in acc.items() if k not in {"cost", "generation_wall_seconds"}},
        "cost": round(float(acc["cost"]), 6),
        "generation_wall_seconds": round(wall, 3),
        "output_tokens_per_wall_second": round(output / wall, 2) if wall > 0 else None,
        "avg_output_tokens_per_turn": round(output / turns, 1) if turns else None,
        "cache_share_percent": round(
            100.0 * acc["cache_read_tokens"] / prompt_material, 1
        ) if prompt_material else None,
    }


def opencode_usage_snapshot(workspace: str | Path) -> dict[str, Any]:
    db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not db.exists():
        return {"available": False, "error": "OpenCode database not found"}
    directory = Path(workspace).as_posix()
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        sessions = list(con.execute(
            """
            SELECT id, title, time_created, time_updated
            FROM session
            WHERE lower(directory) = lower(?)
            ORDER BY time_updated DESC
            """,
            (directory,),
        ))
        if not sessions:
            return {
                "available": True,
                "current_session": None,
                "worktree": _finalize_token_accumulator(_token_accumulator()),
                "session_count": 0,
            }

        latest_id, latest_title, latest_created, latest_updated = sessions[0]
        current = _token_accumulator()
        worktree = _token_accumulator()
        latest_context = 0
        latest_finish = None
        latest_model = None
        latest_provider = None
        first_assistant_ms = None
        last_completed_ms = None

        session_ids = {row[0] for row in sessions}
        for sid, data_text in con.execute(
            "SELECT session_id, data FROM message WHERE session_id IN (%s)"
            % ",".join("?" for _ in session_ids),
            tuple(session_ids),
        ):
            try:
                data = json.loads(data_text)
            except Exception:
                continue
            if data.get("role") != "assistant":
                continue
            _add_message_usage(worktree, data)
            if sid == latest_id:
                _add_message_usage(current, data)
                tokens = data.get("tokens") or {}
                total = int(tokens.get("total") or 0)
                timing = data.get("time") or {}
                created = timing.get("created")
                completed = timing.get("completed")
                if data.get("modelID"):
                    latest_model = data.get("modelID")
                if data.get("providerID"):
                    latest_provider = data.get("providerID")
                if first_assistant_ms is None or (created and created < first_assistant_ms):
                    first_assistant_ms = created
                if completed and (last_completed_ms is None or completed >= last_completed_ms):
                    last_completed_ms = completed
                    latest_context = total
                    latest_finish = data.get("finish")

        tool_counts: dict[str, int] = {}
        tool_calls = 0
        reasoning_parts = 0
        for (data_text,) in con.execute(
            "SELECT data FROM part WHERE session_id = ?",
            (latest_id,),
        ):
            try:
                part = json.loads(data_text)
            except Exception:
                continue
            typ = part.get("type")
            if typ == "tool":
                tool_calls += 1
                tool = str(part.get("tool") or "unknown")
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            elif typ == "reasoning":
                reasoning_parts += 1

        current_final = _finalize_token_accumulator(current)
        worktree_final = _finalize_token_accumulator(worktree)

        model_all = _token_accumulator()
        model_24h = _token_accumulator()
        model_sessions: set[str] = set()
        model_sessions_24h: set[str] = set()
        if latest_model:
            cutoff_ms = int(time.time() * 1000) - 86_400_000
            for sid, created_ms, data_text in con.execute(
                """
                SELECT session_id, time_created, data
                FROM message
                WHERE json_extract(data, '$.role') = 'assistant'
                  AND json_extract(data, '$.providerID') = 'ollama'
                  AND json_extract(data, '$.modelID') = ?
                """,
                (latest_model,),
            ):
                try:
                    data = json.loads(data_text)
                except Exception:
                    continue
                _add_message_usage(model_all, data)
                model_sessions.add(sid)
                if created_ms and created_ms >= cutoff_ms:
                    _add_message_usage(model_24h, data)
                    model_sessions_24h.add(sid)

        model_all_final = _finalize_token_accumulator(model_all)
        model_all_final["session_count"] = len(model_sessions)
        model_24h_final = _finalize_token_accumulator(model_24h)
        model_24h_final["session_count"] = len(model_sessions_24h)

        current_final.update({
            "session_id": latest_id,
            "title": latest_title,
            "time_created_ms": latest_created,
            "time_updated_ms": latest_updated,
            "current_context_tokens": latest_context,
            "latest_finish": latest_finish,
            "model_id": latest_model,
            "provider_id": latest_provider,
            "tool_calls": tool_calls,
            "tool_breakdown": dict(sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "reasoning_parts": reasoning_parts,
            "elapsed_seconds": round(
                max(0, (latest_updated - latest_created) / 1000.0), 1
            ) if latest_created and latest_updated else None,
            "assistant_active_seconds": round(
                max(0, (last_completed_ms - first_assistant_ms) / 1000.0), 1
            ) if first_assistant_ms and last_completed_ms else None,
        })
        return {
            "available": True,
            "current_session": current_final,
            "worktree": worktree_final,
            "session_count": len(sessions),
            "model_all_time": model_all_final,
            "model_24h": model_24h_final,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    finally:
        try:
            con.close()
        except Exception:
            pass
