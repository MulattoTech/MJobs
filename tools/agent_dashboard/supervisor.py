from __future__ import annotations

import hashlib
import json
import os
import re
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
    antigravity_usage_snapshot,
    codex_usage_snapshot,
    log_activity,
    ollama_ps,
    opencode_usage_snapshot,
    prepare_ollama_model,
    process_tree,
    read_codex_rate_limits,
    windows_gpu_snapshot,
    windows_process_snapshot,
)

LANES = ("codex", "antigravity", "ollama", "claude")

DEFAULTS: dict[str, dict[str, Any]] = {
    "codex": {
        "model": "gpt-6-luna",
        "reasoning": "high",
        "auto_continue": True,
        "prompt_file": "prompts/PARALLEL_CODEX_LANE.md",
        "runner": "codex",
        "launch_mode": "terminal",
        "max_subagents": 50,
        "intended_subagents": 8,
    },
    "antigravity": {
        "model": "gemini-3.8-flash-medium",
        "reasoning": "medium",
        "auto_continue": True,
        "prompt_file": "prompts/PARALLEL_ANTIGRAVITY_LANE.md",
        "runner": "agy",
        "launch_mode": "terminal",
    },
    "ollama": {
        "model": "ollama/gemma4:26b",
        "reasoning": "high",
        "auto_continue": False,
        "prompt_file": "prompts/PARALLEL_OLLAMA_LANE.md",
        "runner": "opencode",
        "gpu_offload_percent": 100,
    },
    "claude": {
        "model": "sonnet",
        "reasoning": "high",
        "auto_continue": False,
        "prompt_file": "prompts/dual-agent/claude-release-audit.md",
        "runner": "claude",
        "launch_mode": "terminal",
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
    def __init__(self, root: Path, *, mission_instance: bool = False):
        self.root = root.resolve()
        self.common = git_common_dir(self.root)
        self.bus = self.common / "mjobs-coordination"
        self.bus.mkdir(parents=True, exist_ok=True)
        self.mission_instance = mission_instance
        prefix = "mission-control-v2" if mission_instance else "dashboard"
        self.config_path = self.bus / f"{prefix}.json"
        self.runtime_path = self.bus / f"{prefix}-runtime.json"
        self.log_dir = self.root / "outputs" / ("mission-control-v2" if mission_instance else "agent-dashboard")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.native_log_dir = self.log_dir / "native"
        self.native_log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._children: dict[str, Child] = {}
        self._viewers: dict[str, subprocess.Popen[str]] = {}
        self._stop_requested: set[str] = set()
        self._fast_exits: dict[str, list[float]] = {lane: [] for lane in LANES}
        self._telemetry_cache: dict[str, tuple[float, Any]] = {}
        self._telemetry_inflight: set[str] = set()
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
        if self.mission_instance:
            for item in base["lanes"].values():
                item["auto_continue"] = False
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
            "prompt_override", "gpu_offload_percent", "agent_name",
            "launch_workspace", "launch_profile",
            "launch_mode", "max_subagents", "intended_subagents",
        }
        if "launch_mode" in patch and patch["launch_mode"] not in {"terminal", "headless"}:
            raise ValueError("Select terminal or headless mode")
        if lane != "codex" and any(
            k in patch for k in ("max_subagents", "intended_subagents")
        ):
            raise ValueError("Native Codex subagent controls only apply to the Codex lane")
        if lane == "codex":
            current = self.config["lanes"][lane]
            maximum = patch.get("max_subagents", current.get("max_subagents", 50))
            intended = patch.get("intended_subagents", current.get("intended_subagents", 8))
            if (type(maximum) is not int or not 0 <= maximum <= 50
                    or type(intended) is not int or not 0 <= intended <= maximum):
                raise ValueError("Codex subagent target must be 0..maximum and maximum must be 0..50")
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

    def _prompt(self, lane: str, *, config_override: dict[str, Any] | None = None) -> str:
        cfg = config_override if config_override is not None else self.config["lanes"][lane]
        path = self.root / cfg["prompt_file"]
        base = path.read_text(encoding="utf-8") if path.exists() else ""
        override = (cfg.get("prompt_override") or "").strip()
        suffix = f"\n\nDASHBOARD PROMPT OVERRIDE:\n{override}\n" if override else ""
        if cfg.get("launch_profile"):
            max_subagents = int(cfg.get("max_subagents", 50)) if lane == "codex" else 0
            intended = int(cfg.get("intended_subagents", 8)) if lane == "codex" else 0
            capacity_note = (
                f"CODEX SUBAGENT POLICY FOR THIS LAUNCH: hard maximum {max_subagents} "
                f"concurrent child threads excluding root; intended active target {intended}. "
                "The maximum is enforced by the native Codex CLI configuration override. "
                "The intended target is an orchestration instruction, not a minimum "
                "guarantee or automatic worker spawner. During this authorized "
                "session, delegate promptly to distinct dependency-ready features "
                "with exclusive file/issue/worktree claims, refill safe slots when "
                "productive work remains, and never spawn filler, collide on files, "
                "circumvent account limits or continue unattended. "
                "If target is zero, do not delegate. If maximum is zero, native "
                "multi-agent execution is disabled for this launch.\n\n"
            ) if lane == "codex" else ""
            return (
                "CURRENT EXPLICIT LAUNCH SELECTION: "
                f"model={cfg.get('model')}; reasoning={cfg.get('reasoning')}; "
                f"auto_continue={bool(cfg.get('auto_continue'))}; "
                f"launch_mode={cfg.get('launch_mode', 'terminal')}. "
                "These actual CLI settings supersede incidental model labels in preset profile text.\n\n"
                + capacity_note
                + "USER-STARTED MISSION CONTROL SESSION. Read AGENTS.md, "
                "docs/DUAL_AGENT_COORDINATION.md, planning/lanes/README.md and "
                "docs/PROGRESS_TRACKING.md. You are limited to the exact selected "
                f"lane={lane}, worktree={cfg.get('launch_workspace')}, "
                f"profile={cfg.get('launch_profile')}. "
                f"Session label={cfg.get('agent_name') or 'Default agent'}. "
                "This label identifies the session, not an installed CLI agent. "
                "Git main may be dirty or move: NEVER reset, clean, stash, "
                "force-push, or overwrite another writer. Reconcile inside your "
                "owned lane, identify live conflicting GitHub/local claims, and "
                "acquire a unique exact issue/path claim BEFORE any implementation "
                "mutation. A conflicting claim blocks that feature slice, not "
                "launch itself. Only the sole integrator may edit canonical "
                "planning, shared migrations, release acceptance or main. "
                "Preserve safety gates, no hosted CI, no real application submit. "
                "Do not start unrelated agent fleets. No unattended continuation "
                "unless the current user specifically enabled it.\n\n"
                + base + suffix
            )
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
        native_log_path: Path | None = None,
        config_override: dict[str, Any] | None = None,
    ) -> list[str]:
        cfg = config_override if config_override is not None else self.config["lanes"][lane]
        prompt = self._prompt(lane, config_override=config_override)
        workspace = workspace or self.root
        model = model_override or str(cfg["model"])
        if lane == "codex":
            maximum = int(cfg.get("max_subagents", 50))
            intended = int(cfg.get("intended_subagents", 8))
            if not (0 <= intended <= maximum <= 50):
                raise ValueError("Invalid Codex subagent configuration")
            options = (
                ["-c", "features.multi_agent=false"]
                if maximum == 0 or intended == 0
                else ["-c", "features.multi_agent=true",
                      "-c", f"agents.max_concurrent_threads_per_session={maximum}"]
            )
            # The bare "-" tells Codex to read the COMPLETE approved prompt from
            # stdin. Never interpolate multiline prompts through cmd.exe /c.
            return [
                "codex", "exec", "-C", str(workspace), "-m", model,
                "-c", f'model_reasoning_effort="{cfg.get("reasoning", "high")}"',
                *options, "-s", "danger-full-access", "-",
            ]
        if lane == "antigravity":
            native_log = native_log_path or (self.native_log_dir / "antigravity-preview.log")
            if cfg.get("launch_profile"):
                command = [
                    "agy", "--print", prompt, "--model", model,
                    "--mode", "accept-edits", "--log-file", str(native_log),
                ]
                # Antigravity publishes effort-tier model IDs (e.g. "-medium" or "-high").
                # Those tiers already select the reasoning setting: do not send a
                # conflicting --effort flag for the same model.
                if not re.search(r"-(low|medium|high|xhigh|max)$", model):
                    command.extend(["--effort", str(cfg.get("reasoning", "high"))])
                # Agent name is a session label in the instructions, not an
                # unverified native CLI --agent ID.
                return command
            return [
                "agy", "--print", prompt, "--model", model,
                "--mode", "accept-edits", "--dangerously-skip-permissions",
                "--log-file", str(native_log),
            ]
        if lane == "claude":
            command = [
                "claude", "--print", prompt, "--model", model,
                "--effort", str(cfg.get("reasoning", "high")),
                "--permission-mode", "acceptEdits",
            ]
            # Session label is placed in the instructions, not passed as an
            # unknown --agent name which would cause Claude CLI to fail.
            return command
        return [
            "opencode", "run", "--pure", "--model", model,
            "--auto", "--print-logs", "--log-level", "INFO", "--thinking",
            prompt,
        ]

    def command_preview(self, lane: str) -> list[str]:
        self._require_lane(lane)
        claim = self._live_claim(lane)
        workspace = Path(claim["worktree"]) if claim.get("worktree") else self.root
        command = self._command(lane, workspace)
        if command:
            index = 2 if lane in {"antigravity", "claude"} else len(command) - 1
            command[index] = "<prompt>"
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
        self,
        lane: str,
        workspace: Path,
        model_override: str | None = None,
        native_log_path: Path | None = None,
    ) -> list[str]:
        command = self._command(
            lane,
            workspace,
            model_override=model_override,
            native_log_path=native_log_path,
        )
        return self._runner_prefix(command[0]) + command[1:]

    def _workspace_for_lane(self, lane: str) -> Path:
        cfg = self.config["lanes"][lane]
        if cfg.get("launch_workspace"):
            workspace = Path(str(cfg["launch_workspace"])).resolve()
            if not workspace.is_dir():
                raise RuntimeError("Configured Mission Control worktree is missing; use the launch wizard.")
            return workspace
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

    def open_log_console(self, lane: str) -> dict[str, Any]:
        """View current/previous CLI output in a window without spawning an agent."""
        self._require_lane(lane)
        if os.name != "nt":
            raise RuntimeError("A visible log console is only supported on Windows")
        path = self.log_path(lane)
        if not path.is_file():
            raise RuntimeError("No output log yet. Launch the agent to create one.")
        with self._lock:
            existing = self._viewers.get(lane)
            if existing is not None and existing.poll() is None:
                return {"lane": lane, "pid": existing.pid, "reused": True, "kind": "log_viewer"}
            script = Path(__file__).parent / "terminal_viewer.py"
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 1
            proc = subprocess.Popen(
                [sys.executable, str(script), "--lane", lane, "--log", str(path)],
                cwd=str(self.root), stdin=subprocess.DEVNULL,
                stdout=None, stderr=None, env=dict(os.environ),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                startupinfo=startup,
            )
            self._viewers[lane] = proc
            return {"lane": lane, "pid": proc.pid, "reused": False, "kind": "log_viewer"}

    def log_path(self, lane: str) -> Path:
        return self.log_dir / f"{lane}.log"

    def log_tail(self, lane: str, lines: int = 120) -> str:
        self._require_lane(lane)
        path = self.log_path(lane)
        if not path.exists():
            return ""
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-max(1, min(lines, 1000)):])

    def terminal_sources(self, lane: str) -> dict[str, Any]:
        self._require_lane(lane)
        state = dict(self.runtime["lanes"][lane])
        sources = [{
            "name": "stdout",
            "path": str(self.log_path(lane)),
            "start_offset": int(state.get("log_start_offset") or 0),
        }]
        native = state.get("native_log_path")
        if native:
            sources.append({
                "name": "native",
                "path": str(native),
                "start_offset": 0,
            })
        return {
            "lane": lane,
            "run_id": state.get("run_id"),
            "status": state.get("status"),
            "sources": sources,
        }

    @staticmethod
    def _quota_error_from_text(text: str) -> str | None:
        match = re.search(
            r"(RESOURCE_EXHAUSTED[^\r\n]*Individual quota reached[^\r\n]*)",
            text,
            flags=re.I,
        )
        if match:
            return match.group(1).strip()
        match = re.search(
            r"(Individual quota reached[^\r\n]*Resets in [^\r\n.]*)",
            text,
            flags=re.I,
        )
        return match.group(1).strip() if match else None

    def _live_quota_error(self, lane: str, state: dict[str, Any]) -> str | None:
        entries: list[tuple[Path, int]] = [
            (self.log_path(lane), int(state.get("log_start_offset") or 0))
        ]
        if state.get("native_log_path"):
            entries.append((Path(state["native_log_path"]), 0))
        for path, run_start in entries:
            try:
                if not path.exists():
                    continue
                with path.open("rb") as handle:
                    size = path.stat().st_size
                    handle.seek(max(run_start, size - 262144))
                    text = handle.read().decode("utf-8", errors="replace")
                error = self._quota_error_from_text(text)
                if error:
                    return error
            except Exception:
                continue
        return None

    def start(self, lane: str, *, manual: bool = True,
              expected_prompt_hash: str | None = None) -> dict[str, Any]:
        self._require_lane(lane)
        with self._lock:
            current = self.runtime["lanes"][lane]
            if expected_prompt_hash is not None:
                actual_prompt_hash = hashlib.sha256(
                    self._prompt(lane).encode("utf-8")
                ).hexdigest()
                if actual_prompt_hash != expected_prompt_hash:
                    raise RuntimeError("Prompt changed after review; read and approve it again. Nothing launched.")
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
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            log_path = self.log_path(lane)
            log_start_offset = log_path.stat().st_size if log_path.exists() else 0
            native_log_path = (
                self.native_log_dir / f"{lane}-{run_id}.log"
                if lane == "antigravity" else None
            )
            with open(log_path, "a", encoding="utf-8", buffering=1) as handle:
                handle.write(
                    f"\n\n===== START {utc_now()} workspace={workspace} =====\n"
                )
            cfg = self.config["lanes"][lane]
            mode = str(cfg.get("launch_mode", "terminal"))
            if mode not in {"terminal", "headless"}:
                raise RuntimeError("Unrecognized console launch mode")
            if mode == "terminal" and os.name != "nt":
                raise RuntimeError("Visible Windows terminals require a Windows desktop")
            env = dict(os.environ)
            env["MJOBS_LANE"] = lane
            env["MJOBS_LAUNCH_MODE"] = mode
            command = self._launch_command(
                lane, workspace, model_override=effective_model,
                native_log_path=native_log_path,
            )
            # Single-use manifest avoids passing multiline prompts through
            # Windows cmd.exe/PowerShell argument parsing. The helper deletes
            # the manifest after loading it and copies live stdout to console/log.
            manifest_dir = self.bus / "agent-launch-manifests"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest = manifest_dir / f"{lane}-{run_id}.json"
            self._write_json(manifest, {
                "lane": lane, "argv": command,
                "prompt": self._prompt(lane),
                "workspace": str(workspace), "log": str(log_path),
                "mode": mode, "hold_seconds": 25 if mode == "terminal" else 0,
            })
            helper = Path(__file__).parent / "terminal_host.py"
            flags = (
                subprocess.CREATE_NEW_CONSOLE if mode == "terminal"
                else getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ) if os.name == "nt" else 0
            startup = None
            if mode == "terminal" and os.name == "nt":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 1  # SW_SHOWNORMAL
            try:
                process = subprocess.Popen(
                    [sys.executable, str(helper), "--manifest", str(manifest)],
                    cwd=str(workspace), env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=None if mode == "terminal" else subprocess.DEVNULL,
                    stderr=None if mode == "terminal" else subprocess.DEVNULL,
                    creationflags=flags, startupinfo=startup,
                )
            except BaseException:
                manifest.unlink(missing_ok=True)
                raise
            self._children[lane] = Child(process, None, time.monotonic())
            current.update({
                "pid": process.pid, "status": "RUNNING", "started_at": utc_now(),
                "stopped_at": None, "exit_code": None, "last_reason": None,
                "workspace": str(workspace),
                "effective_model": effective_model,
                "gpu_offload": gpu_offload,
                "launch_mode": mode,
                "terminal_visible": mode == "terminal",
                "run_id": run_id,
                "log_start_offset": log_start_offset,
                "native_log_path": str(native_log_path) if native_log_path else None,
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
                "last_reason": "STOP_REQUESTED",
            })
            child = self._children.pop(lane, None)
            if child and child.log_handle is not None:
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
                    quota_error = self._live_quota_error(lane, state)
                    if quota_error:
                        state["status"] = "BLOCKED_QUOTA"
                        state["last_reason"] = quota_error
                        self.save()
                    else:
                        state["status"] = "RUNNING"
                    return
                runtime_seconds = time.monotonic() - child.started_monotonic
                if child.log_handle is not None:
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

    def _refresh_telemetry_async(self, key: str, loader) -> None:
        try:
            value = loader()
            with self._lock:
                self._telemetry_cache[key] = (time.monotonic(), value)
        except Exception:
            pass
        finally:
            with self._lock:
                self._telemetry_inflight.discard(key)

    def _cached_telemetry(self, key: str, ttl: float, loader):
        now = time.monotonic()
        with self._lock:
            cached = self._telemetry_cache.get(key)
            if cached and (now - cached[0]) < ttl:
                return cached[1]
            if cached:
                if key not in self._telemetry_inflight:
                    self._telemetry_inflight.add(key)
                    threading.Thread(
                        target=self._refresh_telemetry_async,
                        args=(key, loader),
                        daemon=True,
                    ).start()
                return cached[1]

        value = loader()
        with self._lock:
            self._telemetry_cache[key] = (time.monotonic(), value)
        return value

    def telemetry_snapshot(self) -> dict[str, Any]:
        processes = self._cached_telemetry("processes", 2.5, windows_process_snapshot)
        gpu = self._cached_telemetry("gpu", 3.0, windows_gpu_snapshot)
        codex_usage = self._cached_telemetry("codex_usage", 30.0, read_codex_rate_limits)
        ollama = (self._cached_telemetry("ollama_ps", 3.0, ollama_ps)
                  if shutil.which("ollama") else {"available": False, "models": []})
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

    def _lane_file_status(self, lane: str) -> str | None:
        path = self.root / "planning" / "lanes" / lane / "CONTINUATION.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("status")
            return str(value) if value else None
        except Exception:
            return None

    def snapshot(self) -> dict[str, Any]:
        telemetry = self.telemetry_snapshot()
        coordination = self.coordination()
        claims = coordination.get("claims") or {}
        gpu_by_pid = (telemetry.get("gpu") or {}).get("by_pid") or {}
        with self._lock:
            lanes = {}
            for lane in LANES:
                state = dict(self.runtime["lanes"][lane])
                claim = claims.get(lane)
                if (
                    not state.get("pid")
                    and not claim
                    and self._lane_file_status(lane) == "IDLE_READY"
                ):
                    state["status"] = "IDLE_UNASSIGNED"
                    state["last_reason"] = (
                        "Previous work is complete/released; no active issue claim is assigned."
                    )
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
                    usage = dict(telemetry.get("codex_usage") or {"available": False})
                    workspace = state.get("workspace") or (
                        self._live_claim(lane).get("worktree")
                    )
                    if workspace:
                        codex_stats = self._cached_telemetry(
                            f"codex_stats:{workspace}:{activity.get('session_id')}:{self.config['lanes'][lane].get('model')}",
                            3.0,
                            lambda workspace=workspace, session_id=activity.get("session_id"), model=self.config["lanes"][lane].get("model"): codex_usage_snapshot(
                                workspace,
                                session_id=session_id,
                                configured_model=model,
                            ),
                        )
                        usage["telemetry"] = codex_stats
                        current_session = codex_stats.get("current_session") or {}
                        if current_session.get("session_id"):
                            activity["session_id"] = current_session["session_id"]
                    visibility = (
                        "codex exec is a separate one-shot process; it is not guaranteed "
                        "to appear inside another interactive Codex /agents view"
                    )
                elif lane == "antigravity":
                    usage = {
                        "available": False,
                        "message": "Antigravity CLI does not expose a remaining-quota percentage locally.",
                    }
                    workspace = state.get("workspace") or (
                        self._live_claim(lane).get("worktree")
                    )
                    if workspace:
                        agy_stats = self._cached_telemetry(
                            f"antigravity_stats:{workspace}:{activity.get('session_id')}:{self.config['lanes'][lane].get('model')}",
                            3.0,
                            lambda workspace=workspace, conversation_id=activity.get("session_id"), model=self.config["lanes"][lane].get("model"): antigravity_usage_snapshot(
                                workspace,
                                conversation_id=conversation_id,
                                configured_model=model,
                            ),
                        )
                        usage["telemetry"] = agy_stats
                        current_conversation = agy_stats.get("current_conversation") or {}
                        if current_conversation.get("conversation_id"):
                            activity["session_id"] = current_conversation["conversation_id"]
                    visibility = "agy --print supervised process + structured Antigravity transcript telemetry"
                elif lane == "claude":
                    usage = {
                        "available": False,
                        "message": "Claude CLI is supervised locally; token and quota telemetry is not available in this dashboard.",
                    }
                    visibility = "Claude CLI local supervised --print session; no cloud automation or hosted reviews"
                else:
                    workspace = state.get("workspace") or (
                        self._live_claim(lane).get("worktree")
                    )
                    if workspace:
                        usage = self._cached_telemetry(
                            f"opencode_usage:{workspace}",
                            2.5,
                            lambda workspace=workspace: opencode_usage_snapshot(workspace),
                        )
                    else:
                        usage = {"available": False, "error": "No Ollama worktree"}
                    usage["local"] = True
                    usage["message"] = "Local Ollama/OpenCode usage; no cloud quota."
                    loaded_models = (telemetry.get("ollama") or {}).get("models") or []
                    selected_runtime = loaded_models[0] if loaded_models else {}
                    context_raw = selected_runtime.get("context")
                    try:
                        context_window = int(str(context_raw).replace(",", ""))
                    except Exception:
                        context_window = None
                    usage["runtime"] = {
                        "loaded_model": selected_runtime.get("name"),
                        "model_size": selected_runtime.get("size"),
                        "processor": selected_runtime.get("processor"),
                        "context_window": context_window,
                        "keepalive_until": selected_runtime.get("until"),
                    }
                    current = usage.get("current_session") or {}
                    current_tokens = current.get("current_context_tokens")
                    if context_window and current_tokens is not None:
                        current["context_window"] = context_window
                        current["context_used_percent"] = round(
                            100.0 * float(current_tokens) / context_window, 2
                        )
                    observed_gpu = float(
                        (telemetry.get("gpu") or {}).get("utilization_percent") or observed_gpu
                    )
                    visibility = "OpenCode + Ollama supervised process; GPU is shared Ollama runtime"

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
            "coordination": coordination,
            "system": {
                "gpu": telemetry.get("gpu"),
                "ollama": telemetry.get("ollama"),
            },
        }

    @staticmethod
    def _require_lane(lane: str) -> None:
        if lane not in LANES:
            raise KeyError(f"unknown lane {lane!r}")
