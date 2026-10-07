from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .observability import (
    log_activity,
    ollama_ps,
    prepare_ollama_model,
    process_tree,
    read_codex_rate_limits,
    windows_gpu_snapshot,
    windows_process_snapshot,
)

LANES = ("codex", "antigravity", "ollama")

DEFAULTS: dict[str, dict[str, Any]] = {
    "codex": {
        "model": "gpt-6-luna",
        "reasoning": "high",
        "auto_continue": True,
        "prompt_file": "prompts/PARALLEL_CODEX_LANE.md",
        "runner": "codex",
    },
    "antigravity": {
        "model": "gemini-3.8-flash-medium",
        "reasoning": "medium",
        "auto_continue": True,
        "prompt_file": "prompts/PARALLEL_ANTIGRAVITY_LANE.md",
        "runner": "agy",
    },
    "ollama": {
        "model": "ollama/gemma4:26b",
        "reasoning": "high",
        "auto_continue": True,
        "prompt_file": "prompts/PARALLEL_OLLAMA_LANE.md",
        "runner": "opencode",
        "gpu_offload_percent": 100,
    },
}

UNATTENDED_AUTHORITY = """
UNATTENDED EXISTING-ISSUE AUTHORITY:
This lane is executing already-catalogued MJobs issues whose authoritative acceptance
criteria already exist in GitHub/docs/spec. Treat that as implementation work, not
fresh product ideation. Do not invoke brainstorming/writing-plans merely to redesign
an existing requirement and do not stop for routine design/spec/plan approval.
Reuse existing architecture and implement the smallest acceptance-backed vertical
slice. If a genuinely new architectural decision is required beyond repository
authority, checkpoint safely, mark BLOCKED_DESIGN in the lane handoff/log, release
the claim if appropriate, and select another non-overlapping dependency-ready issue
instead of terminating the whole burn-down. After a successful issue/slice, continue
to the next dependency-ready unclaimed issue unless stopped by the user.
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run(args: list[str], cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=str(cwd), text=True, capture_output=True, timeout=timeout,
        encoding="utf-8", errors="replace", check=False,
    )


def git_common_dir(root: Path) -> Path:
    result = _run(["git", "rev-parse", "--git-common-dir"], root)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git common dir unavailable")
    value = Path(result.stdout.strip())
    return value if value.is_absolute() else (root / value).resolve()


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def classify_exit(exit_code: int | None, tail: str) -> str:
    lowered = tail.casefold()
    if any(x in lowered for x in (
        "do you approve", "waiting for your approval", "please approve",
        "blocked_design", "i’m stopping before source changes",
        "i'm stopping before source changes",
    )):
        return "WAITING_APPROVAL"
    if any(x in lowered for x in (
        "usage limit", "rate limit", "quota", "credits exhausted", "usage ran out",
        "resource_exhausted",
    )):
        return "BLOCKED_QUOTA"
    if any(x in lowered for x in ("blocked_environment", "environment blocker")):
        return "BLOCKED_ENVIRONMENT"
    if exit_code == 0:
        return "COMPLETED"
    return "ERROR"


@dataclass
class Child:
    process: subprocess.Popen[str]
    log_handle: Any
    started_monotonic: float


class AgentSupervisor:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.common = git_common_dir(self.root)
        self.bus = self.common / "mjobs-coordination"
        self.bus.mkdir(parents=True, exist_ok=True)
        self.config_path = self.bus / "dashboard.json"
        self.runtime_path = self.bus / "dashboard-runtime.json"
        self.log_dir = self.root / "outputs" / "agent-dashboard"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._children: dict[str, Child] = {}
        self._stop_requested: set[str] = set()
        self._fast_exits: dict[str, list[float]] = {lane: [] for lane in LANES}
        self._telemetry_cache: dict[str, tuple[float, Any]] = {}
        self.config = self._load_config()
        self.runtime = self._load_runtime()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    def _load_config(self) -> dict[str, Any]:
        base = {
            "schema_version": 1,
            "restart_delay_seconds": 3,
            "fast_exit_seconds": 20,
            "max_fast_exits": 3,
            "lanes": {lane: dict(DEFAULTS[lane], prompt_override="") for lane in LANES},
        }
        if self.config_path.exists():
            try:
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                for key in ("restart_delay_seconds", "fast_exit_seconds", "max_fast_exits"):
                    if key in saved:
                        base[key] = saved[key]
                for lane in LANES:
                    base["lanes"][lane].update(saved.get("lanes", {}).get(lane, {}))
            except Exception:
                pass
        self._write_json(self.config_path, base)
        return base

    def _load_runtime(self) -> dict[str, Any]:
        base = {"schema_version": 1, "lanes": {}}
        if self.runtime_path.exists():
            try:
                base = json.loads(self.runtime_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        base.setdefault("lanes", {})
        for lane in LANES:
            base["lanes"].setdefault(lane, {
                "pid": None, "status": "STOPPED", "started_at": None,
                "stopped_at": None, "exit_code": None, "restart_count": 0,
                "last_reason": None,
            })
        self._write_json(self.runtime_path, base)
        return base

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def save(self) -> None:
        self._write_json(self.config_path, self.config)
        self._write_json(self.runtime_path, self.runtime)

    def lane_config(self, lane: str) -> dict[str, Any]:
        self._require_lane(lane)
        return dict(self.config["lanes"][lane])

    def update_config(self, lane: str, patch: dict[str, Any]) -> dict[str, Any]:
        self._require_lane(lane)
        allowed = {
            "model", "reasoning", "auto_continue", "prompt_file",
            "prompt_override", "gpu_offload_percent",
        }
        with self._lock:
            for key, value in patch.items():
                if key in allowed:
                    self.config["lanes"][lane][key] = value
            self.save()
            return self.lane_config(lane)

    def _live_claim(self, lane: str) -> dict[str, Any]:
        path = self.bus / "claims.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("claims", {}).get(lane, {})
        except Exception:
            return {}

    def _issue_brief(self, claim: dict[str, Any]) -> str:
        issue = claim.get("issue")
        if not issue:
            return ""
        result = _run(
            ["gh", "issue", "view", str(issue), "--repo", "MulattoTech/MJobs",
             "--json", "number,title,state,body"],
            self.root,
            timeout=12,
        )
        if result.returncode:
            return (
                f"Issue #{issue} acceptance could not be injected. If needed, run "
                f"'gh issue view {issue} --repo MulattoTech/MJobs' before coding."
            )
        try:
            payload = json.loads(result.stdout)
            body = str(payload.get("body") or "")
            if len(body) > 7000:
                body = body[:7000] + "\n[truncated by supervisor]"
            return (
                f"GitHub issue #{payload.get('number')}: {payload.get('title')} "
                f"[{payload.get('state')}]\n{body}"
            )
        except Exception:
            return result.stdout[:7000]

    def _prompt(self, lane: str) -> str:
        cfg = self.config["lanes"][lane]
        path = self.root / cfg["prompt_file"]
        base = path.read_text(encoding="utf-8") if path.exists() else ""
        override = (cfg.get("prompt_override") or "").strip()
        suffix = f"\n\nDASHBOARD PROMPT OVERRIDE:\n{override}\n" if override else ""
        claim = self._live_claim(lane)
        if claim:
            issue_brief = self._issue_brief(claim)
            focus = (
                f"LANE={lane}. ACTIVE CLAIM: issue #{claim.get('issue')} / "
                f"{claim.get('requirement_id')}; branch={claim.get('branch')}; "
                f"worktree={claim.get('worktree')}. Work this claim only until it is "
                "completed, safely checkpointed, or explicitly released. Do NOT switch "
                "to the repository's global active issue or another lane's work. "
                "FIRST: fetch origin/main; ensure the exact claimed worktree/branch exists "
                "and reconcile it with current main without discarding lane work; then do "
                "all source mutation inside that worktree. "
                f"\n\nAUTHORITATIVE ISSUE CONTRACT:\n{issue_brief}\n"
            )
        else:
            focus = (
                f"LANE={lane}. No live claim is recorded. Select work only from this "
                "lane's own queue after checking other lane claims; do not blindly use "
                "the repository's global active issue. "
            )
        return (
            "EXECUTE NOW. " + focus
            + "This is an implementation turn, not a request to explain the lane "
            "configuration. Use repository tools immediately.\n\n"
            + UNATTENDED_AUTHORITY
            + "\n\nLANE CONTRACT:\n"
            + base
            + suffix
        )

    def _command(
        self,
        lane: str,
        workspace: Path | None = None,
        model_override: str | None = None,
    ) -> list[str]:
        cfg = self.config["lanes"][lane]
        prompt = self._prompt(lane)
        workspace = workspace or self.root
        model = model_override or str(cfg["model"])
        if lane == "codex":
            return [
                "codex", "exec", "-C", str(workspace), "-m", model,
                "-c", f'model_reasoning_effort="{cfg.get("reasoning", "high")}"',
                "-s", "danger-full-access", prompt,
            ]
        if lane == "antigravity":
            return [
                "agy", "--print", prompt, "--model", model,
                "--mode", "accept-edits", "--dangerously-skip-permissions",
            ]
        return [
            "opencode", "run", "--pure", "--model", model,
            "--auto", prompt,
        ]

    def command_preview(self, lane: str) -> list[str]:
        self._require_lane(lane)
        claim = self._live_claim(lane)
        workspace = Path(claim["worktree"]) if claim.get("worktree") else self.root
        command = self._command(lane, workspace)
        if command:
            command[-1] = "<prompt>"
        return command

    def _runner_prefix(self, name: str) -> list[str]:
        direct = shutil.which(name)
        candidates: list[Path] = []
        if direct:
            candidates.append(Path(direct))
        if os.name == "nt":
            appdata = os.environ.get("APPDATA")
            if appdata:
                candidates.extend([
                    Path(appdata) / "npm" / f"{name}.cmd",
                    Path(appdata) / "npm" / f"{name}.exe",
                ])
            local = os.environ.get("LOCALAPPDATA")
            if local and name == "agy":
                packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
                if packages.exists():
                    candidates.extend(packages.glob("*AntigravityCLI*\\agy.exe"))
        for candidate in candidates:
            if not candidate.exists():
                continue
            suffix = candidate.suffix.casefold()
            if suffix in {".cmd", ".bat"}:
                return ["cmd.exe", "/d", "/s", "/c", str(candidate)]
            if suffix == ".ps1":
                return [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(candidate),
                ]
            return [str(candidate)]
        raise FileNotFoundError(
            f"Unable to resolve runner {name!r}. Install it or add it to PATH."
        )

    def _launch_command(
        self, lane: str, workspace: Path, model_override: str | None = None
    ) -> list[str]:
        command = self._command(lane, workspace, model_override=model_override)
        return self._runner_prefix(command[0]) + command[1:]

    def _workspace_for_lane(self, lane: str) -> Path:
        claim = self._live_claim(lane)
        worktree_value = claim.get("worktree")
        branch = claim.get("branch")
        if not worktree_value or not branch:
            raise RuntimeError(
                f"Lane {lane} has no complete active claim/worktree. Claim an issue "
                "before starting a writable supervised lane."
            )
        workspace = Path(str(worktree_value))
        if not workspace.is_absolute():
            workspace = (self.root / workspace).resolve()
        if workspace.exists():
            return workspace
        workspace.parent.mkdir(parents=True, exist_ok=True)
        fetch = _run(["git", "fetch", "origin", "main"], self.root, timeout=30)
        if fetch.returncode:
            raise RuntimeError(fetch.stderr.strip() or "git fetch failed")
        local_branch = _run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            self.root,
        ).returncode == 0
        args = ["git", "worktree", "add"]
        if local_branch:
            args.extend([str(workspace), str(branch)])
        else:
            args.extend(["-b", str(branch), str(workspace), "origin/main"])
        created = _run(args, self.root, timeout=30)
        if created.returncode:
            raise RuntimeError(created.stderr.strip() or created.stdout.strip())
        return workspace

    def log_path(self, lane: str) -> Path:
        return self.log_dir / f"{lane}.log"

    def log_tail(self, lane: str, lines: int = 120) -> str:
        self._require_lane(lane)
        path = self.log_path(lane)
        if not path.exists():
            return ""
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-max(1, min(lines, 1000)):])

    def start(self, lane: str, *, manual: bool = True) -> dict[str, Any]:
        self._require_lane(lane)
        with self._lock:
            current = self.runtime["lanes"][lane]
            if pid_alive(current.get("pid")):
                return dict(current)
            if manual:
                self._stop_requested.discard(lane)
                self._fast_exits[lane].clear()
            workspace = self._workspace_for_lane(lane)
            effective_model = str(self.config["lanes"][lane]["model"])
            gpu_offload = None
            if lane == "ollama":
                effective_model, gpu_offload = prepare_ollama_model(
                    effective_model,
                    int(self.config["lanes"][lane].get("gpu_offload_percent", 100)),
                    self.bus,
                )
            log_path = self.log_path(lane)
            handle = open(log_path, "a", encoding="utf-8", buffering=1)
            handle.write(
                f"\n\n===== START {utc_now()} workspace={workspace} =====\n"
            )
            env = dict(os.environ)
            env["MJOBS_LANE"] = lane
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(
                self._launch_command(lane, workspace, model_override=effective_model),
                cwd=str(workspace), env=env,
                stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                text=True, creationflags=flags,
            )
            self._children[lane] = Child(process, handle, time.monotonic())
            current.update({
                "pid": process.pid, "status": "RUNNING", "started_at": utc_now(),
                "stopped_at": None, "exit_code": None, "last_reason": None,
                "workspace": str(workspace),
                "effective_model": effective_model,
                "gpu_offload": gpu_offload,
            })
            self.save()
            return dict(current)

    def stop(self, lane: str) -> dict[str, Any]:
        self._require_lane(lane)
        with self._lock:
            self._stop_requested.add(lane)
            state = self.runtime["lanes"][lane]
            pid = state.get("pid")
        if pid_alive(pid):
            self._terminate_tree(int(pid))
        with self._lock:
            state.update({
                "pid": None, "status": "STOPPED", "stopped_at": utc_now(),
                "last_reason": "USER_STOP",
            })
            child = self._children.pop(lane, None)
            if child:
                try:
                    child.log_handle.close()
                except Exception:
                    pass
            self.save()
            return dict(state)

    def restart(self, lane: str) -> dict[str, Any]:
        self.stop(lane)
        time.sleep(0.25)
        self._stop_requested.discard(lane)
        return self.start(lane, manual=True)

    def _terminate_tree(self, pid: int) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True)
            for _ in range(20):
                if not pid_alive(pid):
                    return
                time.sleep(0.1)
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    return

    def _monitor_loop(self) -> None:
        while True:
            try:
                self._monitor_once()
            except Exception:
                pass
            time.sleep(1.0)

    def _monitor_once(self) -> None:
        with self._lock:
            lanes = list(LANES)
        for lane in lanes:
            self._inspect_lane(lane)

    def _inspect_lane(self, lane: str) -> None:
        with self._lock:
            state = self.runtime["lanes"][lane]
            pid = state.get("pid")
            child = self._children.get(lane)
            if not pid:
                return
            if child is not None:
                exit_code = child.process.poll()
                if exit_code is None:
                    state["status"] = "RUNNING"
                    return
                runtime_seconds = time.monotonic() - child.started_monotonic
                try:
                    child.log_handle.flush()
                    child.log_handle.close()
                except Exception:
                    pass
                self._children.pop(lane, None)
            else:
                if pid_alive(pid):
                    state["status"] = "ADOPTED_RUNNING"
                    return
                exit_code = state.get("exit_code")
                runtime_seconds = 999.0
            tail = self.log_tail(lane, 100)
            reason = classify_exit(exit_code, tail)
            state.update({
                "pid": None, "status": reason, "stopped_at": utc_now(),
                "exit_code": exit_code, "last_reason": reason,
            })
            now = time.monotonic()
            if runtime_seconds < float(self.config["fast_exit_seconds"]):
                self._fast_exits[lane] = [x for x in self._fast_exits[lane] if now - x < 300]
                self._fast_exits[lane].append(now)
            else:
                self._fast_exits[lane].clear()
            too_fast = len(self._fast_exits[lane]) >= int(self.config["max_fast_exits"])
            auto = bool(self.config["lanes"][lane].get("auto_continue"))
            should_restart = (
                auto and lane not in self._stop_requested and reason == "COMPLETED" and not too_fast
            )
            if too_fast and reason == "COMPLETED":
                state["status"] = "CRASH_LOOP_GUARD"
                state["last_reason"] = "Too many short clean exits; manual review required."
            self.save()
        if should_restart:
            time.sleep(float(self.config["restart_delay_seconds"]))
            with self._lock:
                self.runtime["lanes"][lane]["restart_count"] += 1
            self.start(lane, manual=False)

    def _cached_telemetry(self, key: str, ttl: float, loader):
        now = time.monotonic()
        cached = self._telemetry_cache.get(key)
        if cached and (now - cached[0]) < ttl:
            return cached[1]
        value = loader()
        self._telemetry_cache[key] = (now, value)
        return value

    def telemetry_snapshot(self) -> dict[str, Any]:
        processes = self._cached_telemetry("processes", 2.5, windows_process_snapshot)
        gpu = self._cached_telemetry("gpu", 3.0, windows_gpu_snapshot)
        codex_usage = self._cached_telemetry("codex_usage", 30.0, read_codex_rate_limits)
        ollama = self._cached_telemetry("ollama_ps", 3.0, ollama_ps)
        return {
            "processes": processes,
            "gpu": gpu,
            "codex_usage": codex_usage,
            "ollama": ollama,
        }

    def coordination(self) -> dict[str, Any]:
        script = [sys.executable, "-m", "tools.coordination.lane_coordination", "status"]
        result = _run(script, self.root, timeout=10)
        if result.returncode:
            return {"error": result.stderr.strip(), "raw": result.stdout}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"raw": result.stdout}

    def git_state(self) -> dict[str, Any]:
        branch = _run(["git", "branch", "--show-current"], self.root).stdout.strip()
        head = _run(["git", "rev-parse", "HEAD"], self.root).stdout.strip()
        origin = _run(["git", "rev-parse", "origin/main"], self.root).stdout.strip()
        status = _run(["git", "status", "--short"], self.root).stdout.splitlines()
        worktrees = _run(["git", "worktree", "list"], self.root).stdout.splitlines()
        return {"branch": branch, "head": head, "origin_main": origin, "dirty": status, "worktrees": worktrees}

    def snapshot(self) -> dict[str, Any]:
        telemetry = self.telemetry_snapshot()
        gpu_by_pid = (telemetry.get("gpu") or {}).get("by_pid") or {}
        with self._lock:
            lanes = {}
            for lane in LANES:
                state = dict(self.runtime["lanes"][lane])
                if state.get("pid") and not pid_alive(state["pid"]):
                    state["status"] = "EXITED_UNRECONCILED"
                activity = log_activity(self.log_path(lane), lane)
                tree = process_tree(telemetry.get("processes") or [], state.get("pid"))
                tree_gpu = [
                    float(gpu_by_pid.get(str(item.get("pid")), 0.0))
                    for item in tree
                ]
                observed_gpu = max(tree_gpu, default=0.0)
                age = activity.get("last_activity_seconds")
                if state.get("pid") and age is not None and age <= 20:
                    activity_state = "ACTIVE"
                elif state.get("pid"):
                    activity_state = "QUIET"
                else:
                    activity_state = "STOPPED"

                if lane == "codex":
                    usage = telemetry.get("codex_usage") or {"available": False}
                    visibility = (
                        "codex exec is a separate one-shot process; it is not guaranteed "
                        "to appear inside another interactive Codex /agents view"
                    )
                elif lane == "antigravity":
                    usage = {
                        "available": False,
                        "message": "Antigravity CLI does not expose a quota percentage locally.",
                    }
                    visibility = "agy --print supervised process"
                else:
                    usage = {
                        "available": True,
                        "local": True,
                        "message": "Local Ollama model; no cloud usage quota.",
                    }
                    visibility = "OpenCode + Ollama supervised process"

                lanes[lane] = {
                    "config": self.lane_config(lane),
                    "runtime": state,
                    "command": self.command_preview(lane),
                    "log_tail": self.log_tail(lane, 40),
                    "activity": {
                        **activity,
                        "state": activity_state,
                        "process_tree": tree,
                        "observed_gpu_percent": round(observed_gpu, 1),
                        "visibility_note": visibility,
                    },
                    "usage": usage,
                }
        return {
            "time": utc_now(),
            "lanes": lanes,
            "git": self.git_state(),
            "coordination": self.coordination(),
            "system": {
                "gpu": telemetry.get("gpu"),
                "ollama": telemetry.get("ollama"),
            },
        }

    @staticmethod
    def _require_lane(lane: str) -> None:
        if lane not in LANES:
            raise KeyError(f"unknown lane {lane!r}")
