"""Read-only acceptance status and safe, explicit local agent launching for Mission Control.

The launcher NEVER changes root main, resets other worktrees, claims other owners'
files, or pushes. Users explicitly start each paid/external coding CLI.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hmac
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tools.verification.progress_card import card_current
from tools.verification.progress_dashboard import snapshot

PROFILE_ROWS = (
    ("codex", "codex-luna-high", "Luna High — Greenhouse application workflow", "gpt-6-luna", "high"),
    ("codex", "codex-sol-6-1-high", "Sol 6.1 High — resilient applications", "", "high"),
    ("codex", "codex-astra-high", "Astra High — adapter and form review", "", "high"),
    ("antigravity", "antigravity-opus", "Opus — explainable AI matching", "claude-opus-4-6-thinking", "high"),
    ("antigravity", "antigravity-sonnet", "Sonnet — weighted scoring", "claude-sonnet-4-6", "high"),
    ("antigravity", "antigravity-gemini-3-8-flash-medium", "Gemini 3.8 Flash Medium — scoring components", "gemini-3.8-flash-medium", "medium"),
    ("antigravity", "antigravity-gemini-3-8-flash-high", "Gemini 3.8 Flash High — scoring pipeline", "gemini-3.8-flash-high", "high"),
    ("claude", "claude-release-audit", "Claude — independent release/test gap validation", "sonnet", "high"),
    ("claude", "claude-test-harness", "Claude — controlled test-harness implementation", "opus", "high"),
)
PROFILES = {slug: {
    "lane": lane,
    "slug": slug,
    "label": label,
    "default_model": model,
    "effort": effort,
    "path": f"prompts/dual-agent/{slug}.md",
} for lane, slug, label, model, effort in PROFILE_ROWS}
LAUNCH_LANES = ("codex", "antigravity", "claude")
WORKTREES = {
    "codex": ("MJobs-Codex-App", "codex/priority-app-automation-20261009"),
    "antigravity": ("MJobs-Antigravity-Matching", "antigravity/priority-matching-ai-20261009"),
    "claude": ("MJobs-Claude-Delivery", "claude/priority-delivery-tests-20261009"),
}
RUNNERS = {"codex": "codex", "antigravity": "agy", "claude": "claude"}


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
    agent: str = Field(default="", max_length=80)
    instructions: str = Field(default="", max_length=6000)
    auto_continue: bool = False


class MissionControl:
    def __init__(self, supervisor: Any):
        self.supervisor = supervisor
        self.root = supervisor.root
        self.token = secrets.token_urlsafe(32)
        self.mutex = threading.RLock()
        self._remote_cache: tuple[float, str | None, str] | None = None

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
        if lane not in LAUNCH_LANES:
            raise ValueError("Unknown implementation lane")
        suffix, branch = WORKTREES[lane]
        return self.root.parent / suffix, branch

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
                "name": {"codex": "Codex CLI", "antigravity": "Antigravity CLI", "claude": "Claude CLI"}[lane],
                "available": self.models_available(lane),
                "worktree": str(self.workspace(lane)[0]),
                "branch": self.workspace(lane)[1],
                "config": self.supervisor.lane_config(lane),
                "runtime": dict(self.supervisor.runtime["lanes"][lane]),
            } for lane in LAUNCH_LANES],
            "legacy_opencode_available": bool(shutil.which("opencode") and shutil.which("ollama")),
            "policy": "Never reset, stash, merge, or push root main during launch. Own files and claims before coding.",
        }

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

    def launch(self, lane: str, config: LaunchRequest) -> dict[str, Any]:
        if lane not in LAUNCH_LANES:
            raise ValueError("Unknown CLI slot")
        profile = PROFILES.get(config.profile)
        if profile is None or profile["lane"] != lane:
            raise ValueError("The selected profile does not belong to this CLI.")
        if config.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Unsupported reasoning effort")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,119}", config.model):
            raise ValueError("Use an exact model ID, without shell characters or spaces.")
        if config.agent and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", config.agent):
            raise ValueError("Invalid CLI agent name")
        if not (self.root / profile["path"]).is_file():
            raise ValueError("Profile file is missing from the repository.")
        with self.mutex:
            state = self.supervisor.runtime["lanes"][lane]
            from tools.agent_dashboard.supervisor import pid_alive
            if pid_alive(state.get("pid")):
                raise RuntimeError("This CLI is already running. Stop it first; no implicit restart or session takeover.")
            self.supervisor._runner_prefix(RUNNERS[lane])
            prepared = self.prepare(lane)
            patch = {
                "launch_workspace": prepared["workspace"],
                "launch_profile": profile["slug"],
                "prompt_file": profile["path"],
                "model": config.model,
                "reasoning": config.effort,
                "agent_name": config.agent,
                "prompt_override": config.instructions,
                "auto_continue": bool(config.auto_continue),
            }
            self.supervisor.update_config(lane, patch)
            running = self.supervisor.start(lane, manual=True)
            return {"status": running["status"], "pid": running["pid"], "lane": lane,
                    "profile": profile["slug"], "preflight": prepared}

    def stop(self, lane: str) -> dict[str, Any]:
        if lane not in LAUNCH_LANES:
            raise ValueError("Unknown launch lane")
        return self.supervisor.stop(lane)
