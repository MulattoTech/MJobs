from __future__ import annotations

import json
import os
import queue
import re
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
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        tail = "\n".join(lines)
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
