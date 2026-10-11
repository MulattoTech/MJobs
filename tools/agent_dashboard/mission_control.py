"""Read-only acceptance status and safe, explicit local agent launching for Mission Control.

The launcher NEVER changes root main, resets other worktrees, claims other owners'
files, or pushes. Users explicitly start each paid/external coding CLI.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import hmac
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .model_catalog import ModelCatalog
from .agent_environment import resolve_python_environment
from tools.verification.progress_card import card_current
from tools.verification.progress_dashboard import snapshot

PROFILE_ROWS = (
    ("codex", "codex-general", "General MJobs development", "gpt-6.1-sol", "high"),
    ("codex", "codex-luna-high", "Application workflow and recovery", "gpt-6-luna", "high"),
    ("codex", "codex-sol-6-1-high", "Safety and core integration", "gpt-6.1-sol", "high"),
    ("codex", "codex-astra-high", "Adapters and frontend", "gpt-6-astra", "high"),
)
PROFILES = {slug: {
    "lane": lane,
    "slug": slug,
    "label": label,
    "default_model": model,
    "effort": effort,
    "path": f"prompts/codex/{slug}.md",
} for lane, slug, label, model, effort in PROFILE_ROWS}
LAUNCH_LANES = ("codex",)
WORKTREES = {"codex": ("MJobs", "main")}
RUNNERS = {"codex": "codex"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def git(cwd: Path, *args: str, timeout: float = 16.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, encoding="utf-8",
        errors="replace", text=True, check=False, timeout=timeout,
    )


def git_text(cwd: Path, *args: str) -> str:
    r = git(cwd, *args)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:450] or "Git command failed")
    return r.stdout.strip()


class LaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str
    model: str = Field(min_length=1, max_length=120)
    effort: str = Field(default="high")
    agent: str = Field(default="", max_length=80)  # Safe session label, NOT an arbitrary CLI --agent
    instructions: str = Field(default="", max_length=6000)
    auto_continue: bool = False
    launch_mode: Literal["terminal", "headless"] = "terminal"
    max_subagents: int = Field(default=50, ge=0, le=50)
    intended_subagents: int = Field(default=8, ge=0, le=50)
    preview_hash: str = Field(default="", max_length=64)


class MissionControl:
    def __init__(self, supervisor: Any):
        self.supervisor = supervisor
        self.root = supervisor.root
        self.token = secrets.token_urlsafe(32)
        self.mutex = threading.RLock()
        self._remote_cache: tuple[float, str | None, str] | None = None
        self.models = ModelCatalog(supervisor)

    @staticmethod
    def require_token(request: Request, token: str) -> None:
        if not token or not hmac.compare_digest(request.headers.get("x-mjobs-launch-token", ""), token):
            raise HTTPException(status_code=403, detail="Open Mission Control itself to authorize this action.")
        origin = request.headers.get("origin")
        if origin:
            allowed = {f"{request.url.scheme}://{request.url.netloc}"}
            if origin not in allowed:
                raise HTTPException(status_code=403, detail="Cross-origin process control is not allowed.")

    def workspace(self, lane: str) -> tuple[Path, str]:
        if lane != "codex":
            raise ValueError("Only the Codex integration slot is supported")
        return self.root, "main"

    def models_available(self, lane: str) -> bool:
        try:
            self.supervisor._runner_prefix(RUNNERS[lane])
            return True
        except (FileNotFoundError, OSError):
            return False

    def options(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "profiles": list(PROFILES.values()),
            "lanes": [{
                "lane": lane,
                "name": "Codex CLI",
                "available": self.models_available(lane),
                "worktree": str(self.workspace(lane)[0]),
                "branch": self.workspace(lane)[1],
                "config": self.supervisor.lane_config(lane),
                "runtime": dict(self.supervisor.runtime["lanes"][lane]),
                "catalog": self.models.get(lane),
            } for lane in LAUNCH_LANES],
            "legacy_opencode_available": False,
            "model_selector_version": 4,
            "mission_instance": bool(self.supervisor.mission_instance),
            "policy": "One Codex development workspace. No unattended execution, automatic claims, or changes to main during launch.",
        }

    def refresh_models(self) -> dict[str, Any]:
        """Explicit manual model discovery, without launching or changing any CLI session."""
        return {lane: self.models.get(lane, refresh=True) for lane in LAUNCH_LANES}

    def _remote_sha(self) -> tuple[str | None, str]:
        now = time.monotonic()
        if self._remote_cache is not None and now - self._remote_cache[0] < 40:
            return self._remote_cache[1:]
        try:
            res = git(self.root, "ls-remote", "origin", "refs/heads/main", timeout=5)
            match = re.match(r"^([0-9a-f]{40})\s", res.stdout)
            sha = match.group(1) if res.returncode == 0 and match else None
            note = "GitHub checked" if sha else "GitHub unavailable; local acceptance shown"
        except (OSError, subprocess.TimeoutExpired):
            sha, note = None, "GitHub check timed out; local acceptance shown"
        self._remote_cache = (now, sha, note)
        return sha, note

    def progress(self) -> dict[str, Any]:
        try:
            state = snapshot(self.root)
            def counts(items, total):
                return {
                    "pass": int(items.get("PASS", 0)),
                    "partial": int(items.get("PARTIAL", 0)),
                    "not_run": int(items.get("NOT_RUN", 0)),
                    "total": total,
                    "accepted_percent": round(100 * int(items.get("PASS", 0)) / total, 1) if total else 0,
                }
            original = counts(state["original"], state["original_total"])
            reference = counts(state["reference"], state["reference_total"])
            areas = [{"name": name, **counts(values, sum(values.values()))}
                     for name, values in state["areas"].items()]
            priorities = [{"name": name, **counts(values, sum(values.values()))}
                          for name, values in state["priorities"].items()]
            head = git_text(self.root, "rev-parse", "HEAD")
            remote_sha, remote_note = self._remote_sha()
            from tools.verification.progress_dashboard import DASHBOARD, render_dashboard
            dashboard = self.root / DASHBOARD
            checkpoint_current = (
                dashboard.is_file()
                and dashboard.read_text(encoding="utf-8") == render_dashboard(self.root)
                and card_current(self.root, state)
            )
            return {
                "available": True, "observed_at": utc_now(),
                "source": "LIVE LOCAL CANONICAL MATRICES",
                "original": original, "reference": reference,
                "original_as_of": state["original_as_of"],
                "reference_as_of": state["reference_as_of"],
                "milestone": state["milestone"],
                "release_readiness": state["release"],
                "checkpoint_current": checkpoint_current,
                "areas": sorted(areas, key=lambda x: (x["accepted_percent"], x["name"])),
                "priorities": sorted(priorities, key=lambda x: x["name"]),
                "git": {
                    "local_main_sha": head,
                    "github_main_sha": remote_sha,
                    "published_matches_local_main": head == remote_sha if remote_sha else None,
                    "note": remote_note,
                },
                "caveat": "Only fully accepted requirements count. Partial work is not a fractional PASS; release readiness is separate.",
            }
        except (ValueError, KeyError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "observed_at": utc_now(), "error": str(exc)[:500]}

    def prepare(self, lane: str) -> dict[str, Any]:
        """Reuse lane files without altering them; bootstrap only missing isolated worktrees."""
        worktree, branch = self.workspace(lane)
        warnings: list[str] = []
        expected_common = self.supervisor.common.resolve()
        base = "refs/remotes/origin/main"
        fetch_ok = False
        try:
            fetched = git(self.root, "fetch", "origin", "main", timeout=17)
            fetch_ok = fetched.returncode == 0
            if not fetch_ok:
                warnings.append("GitHub fetch failed; using existing local tracking reference.")
        except (OSError, subprocess.TimeoutExpired):
            warnings.append("GitHub fetch unavailable; using existing tracked or local base.")
        if git(self.root, "rev-parse", "--verify", base).returncode:
            base = "refs/heads/main"
            warnings.append("No cached origin/main; starting from local main without modifying it.")
        if git(self.root, "rev-parse", "--verify", base).returncode:
            raise RuntimeError("No committed main exists to safely start a worktree.")
        if worktree.exists():
            if not worktree.is_dir():
                raise RuntimeError(f"Reserved lane path exists but is not a directory: {worktree}")
            common = Path(git_text(worktree, "rev-parse", "--git-common-dir"))
            if not common.is_absolute():
                common = (worktree / common).resolve()
            if common.resolve() != expected_common:
                raise RuntimeError("The designated worktree belongs to a different repository; refusing to touch it.")
            current = git_text(worktree, "branch", "--show-current")
            if current != branch:
                raise RuntimeError(f"Existing lane checkout is on {current!r}, expected {branch!r}. Preserve it and inspect manually.")
        else:
            worktree.parent.mkdir(parents=True, exist_ok=True)
            exists = git(self.root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
            args = ("worktree", "add", str(worktree), branch) if exists else (
                "worktree", "add", "-b", branch, str(worktree), base,
            )
            result = git(self.root, *args, timeout=70)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout)[:600] or "Failed creating isolated worktree")
        dirty = git_text(worktree, "status", "--porcelain")
        ahead, behind = 0, 0
        difference = git(worktree, "rev-list", "--left-right", "--count", f"{base}...HEAD")
        if difference.returncode == 0:
            try:
                behind, ahead = (int(x) for x in difference.stdout.split())
            except (ValueError, TypeError):
                warnings.append("Cannot classify current lane ancestry.")
        # Even ignored files are user data: never auto-advance over an ignored build artifact.
        all_local = git(worktree, "status", "--porcelain", "--ignored=matching",
                        "--untracked-files=normal", timeout=20)
        entirely_empty = all_local.returncode == 0 and not all_local.stdout.strip()
        if behind and not ahead and entirely_empty:
            ff = git(worktree, "merge", "--ff-only", base, timeout=25)
            if ff.returncode == 0:
                behind = 0
            else:
                warnings.append("Empty lane could not fast-forward; preserving it without reset.")
        elif behind:
            warnings.append("Lane has local files or diverged history. Reconcile it deliberately; no reset or automatic merge.")
        if dirty:
            warnings.append("Lane contains uncommitted work; it was preserved without stashing or cleaning.")
        if not fetch_ok:
            warnings.append("Launch is allowed offline but GitHub state may be stale.")
        return {
            "workspace": str(worktree),
            "branch": branch,
            "head": git_text(worktree, "rev-parse", "HEAD"),
            "ahead": ahead, "behind": behind, "dirty": bool(dirty),
            "warnings": warnings,
        }

    def _validated_selection(
        self, lane: str, config: LaunchRequest,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Pure launch validation shared by preview and actual execution."""
        if lane not in LAUNCH_LANES:
            raise ValueError("Unknown CLI slot")
        profile = PROFILES.get(config.profile)
        if profile is None or profile["lane"] != lane:
            raise ValueError("The selected profile does not belong to this CLI.")
        chosen_model = self.models.validate(lane, config.model, config.effort)
        if chosen_model["catalog"].get("stale"):
            raise ValueError("Model catalog is stale. Refresh models and review the prompt again before launching.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,119}", config.model):
            raise ValueError("Use an exact model ID, without shell characters or spaces.")
        if config.agent and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}", config.agent):
            raise ValueError("Invalid agent session label; use letters, digits, spaces, underscore, dots or hyphens.")
        if not (self.root / profile["path"]).is_file():
            raise ValueError("Profile file is missing from the repository.")
        if lane == "codex" and config.intended_subagents > config.max_subagents:
            raise ValueError("Intended active subagents cannot exceed the maximum.")
        return profile, chosen_model

    @staticmethod
    def _launch_patch(config: LaunchRequest, profile: dict[str, Any],
                      workspace: Path) -> dict[str, Any]:
        patch = {
            "launch_workspace": str(workspace),
            "launch_profile": profile["slug"],
            "prompt_file": profile["path"],
            "model": config.model,
            "reasoning": config.effort,
            "agent_name": config.agent,
            "prompt_override": config.instructions,
            "auto_continue": bool(config.auto_continue),
            "launch_mode": config.launch_mode,
        }
        if profile["lane"] == "codex":
            patch.update({
                "max_subagents": config.max_subagents,
                "intended_subagents": config.intended_subagents,
            })
        return patch

    def preview(self, lane: str, config: LaunchRequest) -> dict[str, Any]:
        """Assemble the EXACT effective prompt and CLI arguments without launching.

        No worktree, user settings, command process, or existing agent is mutated.
        The resulting fingerprint binds all selected arguments, prompt bytes, and
        the intended workspace to a launch request; any change invalidates it.
        """
        profile, selected = self._validated_selection(lane, config)
        workspace, branch = self.workspace(lane)
        patch = self._launch_patch(config, profile, workspace)
        effective = self.supervisor.lane_config(lane)
        effective.update(patch)
        prompt = self.supervisor._prompt(lane, config_override=effective)
        argv = self.supervisor._command(
            lane, workspace, config_override=effective,
        )
        # Read-only dependency preflight. Highlight the exact Python interpreter
        # that future test commands will resolve, before spending model credits.
        python_environment = {"available": None, "message": "Not inspected"}
        if (self.root / "requirements.lock").is_file():
            try:
                selected_python = resolve_python_environment(self.root, workspace)
                python_environment = {"available": True, **selected_python}
            except RuntimeError as exc:
                python_environment = {"available": False, "message": str(exc)}
        command = list(argv)
        if lane == "codex":
            if not command or command[-1] != "-":
                raise RuntimeError("Codex must receive its full approved prompt via stdin")
            command[-1] = "<approved prompt via stdin>"
        payload = {
            "lane": lane,
            "profile": profile["slug"],
            "model": config.model,
            "effort": config.effort,
            "agent": config.agent,
            "instructions": config.instructions,
            "auto_continue": bool(config.auto_continue),
            "launch_mode": config.launch_mode,
            "max_subagents": config.max_subagents if lane == "codex" else None,
            "intended_subagents": config.intended_subagents if lane == "codex" else None,
            "workspace": str(workspace),
            "branch": branch,
            "python_environment": python_environment.get("interpreter") or python_environment.get("message"),
            "prompt": prompt,
            "command": command,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return {
            "preview_hash": fingerprint,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt": prompt,
            "profile": profile["slug"],
            "profile_label": profile["label"],
            "profile_path": profile["path"],
            "model": config.model,
            "effort": config.effort,
            "agent": config.agent or "Default agent",
            "auto_continue": bool(config.auto_continue),
            "launch_mode": config.launch_mode,
            "max_subagents": config.max_subagents if lane == "codex" else None,
            "intended_subagents": config.intended_subagents if lane == "codex" else None,
            "workspace": str(workspace),
            "branch": branch,
            "command": command,
            "python_environment": python_environment,
            "warnings": (
                ["The installed CLI may require authentication or model entitlement at launch."]
                + (["This worktree does not exist yet; it will be safely created at launch."]
                   if not workspace.exists() else [])
                + ([python_environment["message"]]
                   if python_environment.get("available") is False else [])
            ),
        }

    def launch(self, lane: str, config: LaunchRequest) -> dict[str, Any]:
        """Fail closed unless the user approved the exact current prompt."""
        with self.mutex:
            approved = self.preview(lane, config)
            if (not config.preview_hash
                    or not hmac.compare_digest(config.preview_hash, approved["preview_hash"])):
                raise ValueError(
                    "This selection has not been reviewed or has changed. "
                    "Click Read prompt, review the full instructions, approve them, then launch."
                )
            state = self.supervisor.runtime["lanes"][lane]
            from tools.agent_dashboard.supervisor import pid_alive
            if pid_alive(state.get("pid")):
                raise RuntimeError(
                    "This CLI is already running. Stop it first; no implicit restart or session takeover."
                )
            # Execution capabilities and writeable worktree preparation are
            # deliberately postponed until after successful preview validation.
            self.supervisor._runner_prefix(RUNNERS[lane])
            prepared = self.prepare(lane)
            profile = PROFILES[config.profile]
            self.supervisor.update_config(
                lane, self._launch_patch(config, profile, Path(prepared["workspace"])),
            )
            running = self.supervisor.start(
                lane, manual=True, expected_prompt_hash=approved["prompt_sha256"],
            )
            return {
                "status": running["status"], "pid": running["pid"],
                "lane": lane, "profile": profile["slug"],
                "preflight": prepared,
                "approved_preview_hash": approved["preview_hash"],
            }

    def stop(self, lane: str) -> dict[str, Any]:
        if lane not in LAUNCH_LANES:
            raise ValueError("Unknown launch lane")
        return self.supervisor.stop(lane)
