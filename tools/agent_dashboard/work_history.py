"""Read-only, low-overhead MJobs development history for Mission Control.

Git commits are independently verifiable work checkpoints. Codex turns are
separate observations: timestamps never establish which agent authored a
commit. This reader does not mutate Git, contact a model, write records, spawn
agents or change coordination. GitHub-main mode reads commit metadata via gh CLI.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any

from .github_history import fetch_github_main

MAX_COMMITS = 100
REFRESH_SECONDS = 5
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Classification uses actual committed paths, NOT just commit subject labels.
PRODUCT_PREFIXES = ("apps/", "services/", "packages/", "workers/", "migrations/", "cli/")
OPS_PREFIXES = ("tools/", "infra/", ".github/", ".devcontainer/")
PLAN_PREFIXES = ("planning/", "docs/evidence/")
TEST_RE = re.compile(r"(^tests?/|/tests?/|(?:\.test\.|\.spec\.|/test_[^/]+\.py$))", re.I)
FEATURE_RE = re.compile(r"^(feat(?:\([^)]*\))?:|add\b|build\b|implement\b|introduce\b|support\b|enable\b|connect\b)", re.I)
FIX_RE = re.compile(r"^(fix(?:\([^)]*\))?:|fix\b|repair\b|resolve\b|correct\b)", re.I)
BOOKKEEPING_RE = re.compile(
    r"^(?:docs?\((?:acceptance|progress|evidence|checkpoint|planning|integration)\):"
    r"|record\b|checkpoint\b|reconcile\b|refresh progress\b|update tracker\b)", re.I
)
ISSUE_RE = re.compile(r"(?:#\d{1,6}|MJ-(?:REQ|REF)-\d{3}(?:\.[A-Za-z0-9_-]+)?)", re.I)


def path_type(path: str) -> str:
    p = path.replace("\\", "/").lower()
    if TEST_RE.search(p) or p.startswith("tools/verification/"):
        return "TEST"
    if p.startswith(PLAN_PREFIXES) or (
        p.startswith("docs/") and any(s in p for s in (
            "progress", "checkpoint", "acceptance", "release_receipt", "session_handoff",
        ))
    ):
        return "BOOKKEEPING"
    if p.endswith((".md", ".rst")) or p.startswith(("docs/", "prompts/", ".agents/")):
        return "DOCUMENTATION"
    # The control center's GUI/server itself is a user-visible MJobs product feature.
    if p.startswith("tools/agent_dashboard/"):
        return "PRODUCT"
    if p.startswith(PRODUCT_PREFIXES):
        return "PRODUCT"
    if p.startswith(OPS_PREFIXES) or p.endswith((".ps1", ".sh", ".bat", ".cmd")):
        return "OPERATIONS"
    return "OTHER"


def project_area(paths: list[str]) -> str:
    candidates = [p.lower().replace("\\", "/") for p in paths]
    rules = (
        ("Dashboard", ("tools/agent_dashboard/", "mission_control", "agent_dashboard")),
        ("Applications", ("services/applications/", "apps/api/dynamic_application", "application-form", "mock_adapter", "application_")),
        ("Job discovery", ("services/discovery/", "source_candidates/", "source-candidate", "job-search")),
        ("Matching", ("services/matching/", "apps/api/match", "match-feedback")),
        ("Resumes and documents", ("services/documents/", "career_profile/", "resume", "cover_letter", "document-history")),
        ("Tracking", ("services/tracking/", "interview_preparation", "application_status")),
        ("Agents", ("services/agents/", "agent_runtime")),
        ("Persistence", ("packages/persistence/", "migrations/")),
        ("Web interface", ("apps/web/",)),
        ("API", ("apps/api/",)),
        ("Background workers", ("workers/",)),
    )
    for label, needles in rules:
        if any(any(s in path for s in needles) for path in candidates):
            return label
    return "Product" if any(path_type(p) == "PRODUCT" for p in paths) else "Repository"


def classify_commit(subject: str, files: list[dict[str, Any]]) -> tuple[str, str]:
    """Structure-based label, NOT proof that full product acceptance passed."""
    paths = [f["path"] for f in files]
    types = Counter(path_type(p) for p in paths)
    product = [f for f in files if path_type(f["path"]) == "PRODUCT"]
    runtime_change = sum(f["additions"] + f["deletions"] for f in product)
    if product:
        if FIX_RE.match(subject):
            return "BUGFIX", "Production defect repair"
        if FEATURE_RE.match(subject):
            layers = set()
            for f in product:
                p = f["path"].lower().replace("\\", "/")
                if p.startswith("apps/web/"):
                    layers.add("web")
                elif p.startswith("apps/api/"):
                    layers.add("api")
                elif p.startswith("migrations/") or p.startswith("packages/persistence/"):
                    layers.add("storage")
                elif p.startswith("services/"):
                    layers.add("services")
                elif p.startswith("workers/"):
                    layers.add("workers")
                elif p.startswith("cli/"):
                    layers.add("cli")
            if runtime_change >= 120 and len(layers) >= 2:
                return "CORE_FEATURE", "Multi-component product feature"
            return "PRODUCT_FEATURE", "Product feature implementation"
        return "PRODUCT_CHANGE", "Production implementation change"
    if types["TEST"]:
        return "TEST", "Test and verification work"
    if types["OPERATIONS"]:
        return "OPERATIONS", "Engineering tooling or configuration"
    if (types["BOOKKEEPING"] or BOOKKEEPING_RE.match(subject)) and (
        BOOKKEEPING_RE.match(subject) or not types["DOCUMENTATION"]
    ):
        return "BOOKKEEPING", "Planning, evidence and checkpoints"
    if types["DOCUMENTATION"]:
        return "DOCUMENTATION", "Guides and written documentation"
    if types["BOOKKEEPING"]:
        return "BOOKKEEPING", "Planning, evidence and checkpoints"
    return "OTHER", "Other committed changes"


def parse_git_log(output: str) -> list[dict[str, Any]]:
    """Parse a bounded, line-based Git numstat log. No shell or eval."""
    results: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish(item: dict[str, Any] | None):
        if item is None:
            return
        files = item["files"]
        category, explanation = classify_commit(item["subject"], files)
        item["category"] = category
        item["category_explanation"] = explanation
        item["area"] = project_area([f["path"] for f in files])
        item["issue_refs"] = list(dict.fromkeys(ISSUE_RE.findall(item["subject"])))[:8]
        item["files_changed"] = len(files)
        item["added"] = sum(f["additions"] for f in files)
        item["deleted"] = sum(f["deletions"] for f in files)
        item["production_lines_changed"] = sum(
            f["additions"] + f["deletions"] for f in files if path_type(f["path"]) == "PRODUCT"
        )
        # The complete paths were used for classification; limit UI payload.
        item["files"] = files[:30]
        item["files_truncated"] = max(0, len(files) - len(item["files"]))
        results.append(item)

    for line in output.splitlines():
        if line.startswith("__MJH__"):
            finish(current)
            current = None
            parts = line[7:].split("\x1f", 3)
            if len(parts) != 4 or not re.fullmatch(r"[0-9a-fA-F]{40}", parts[0]):
                continue
            sha, timestamp, author, title = parts
            current = {
                "sha": sha,
                "short_sha": sha[:10],
                "timestamp": timestamp,
                "author": author[:120],
                "subject": title[:260],
                "files": [],
            }
        elif current is not None and "\t" in line:
            data = line.split("\t", 2)
            if len(data) != 3:
                continue
            add, delete, path = data
            if not path:
                continue
            current["files"].append({
                "path": path[:440],
                "additions": int(add) if add.isdigit() else 0,
                "deletions": int(delete) if delete.isdigit() else 0,
                "kind": path_type(path),
                "binary": not(add.isdigit() and delete.isdigit()),
            })
    finish(current)
    return results


def run_git(root: Path, *args: str, timeout: float = 9.0) -> str:
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=NO_WINDOW, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Read-only Git history temporarily unavailable") from exc
    if proc.returncode:
        raise RuntimeError("Cannot read local Git history; verify the selected MJobs checkout")
    return proc.stdout


def session_activity(root: Path, *, codex_home: Path | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """Bounded root turn *observation*, not commit authorship attribution.

    Only metadata fields, never prompts, messages, account secrets or notes.
    Scan at most 250 candidate logs and <=1.5 MB from the newest root log.
    """
    base = codex_home or Path.home() / ".codex"
    now = now or datetime.now().astimezone()
    files: list[Path] = []
    for offset in range(5):
        day = now - timedelta(days=offset)
        folder = base / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if folder.exists():
            files.extend(folder.glob("*.jsonl"))
    try:
        files.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    except OSError:
        return {"available": False}
    expected = str(root.resolve()).lower().replace("\\", "/").rstrip("/")
    selected = None
    model = effort = None
    for file in files[:250]:
        try:
            with file.open("rb") as inp:
                first = json.loads(inp.readline())
                if first.get("type") != "session_meta":
                    continue
                meta = first.get("payload") or {}
                cwd = str(meta.get("cwd") or "").lower().replace("\\", "/").rstrip("/")
                source = meta.get("source")
                if cwd != expected or (isinstance(source, dict) and source.get("subagent")):
                    continue
                selected = (file, meta)
                for _ in range(90):
                    line = inp.readline()
                    if not line:
                        break
                    if b'"type":"turn_context"' in line:
                        obj = json.loads(line)
                        info = obj.get("payload") or {}
                        model = info.get("model")
                        effort = info.get("effort")
                        break
                break
        except (OSError, ValueError, TypeError):
            continue
    if selected is None:
        return {"available": False, "note": "No recent root-session metadata found"}
    file, meta = selected
    turn_times: list[str] = []
    last_token_at = None
    try:
        with file.open("rb") as handle:
            total = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, total - 1_500_000), os.SEEK_SET)
            if total > 1_500_000:
                handle.readline()  # Skip an incomplete first JSONL line.
            for line in handle:
                if b'"turn_context"' not in line and b'"token_count"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                stamp = record.get("timestamp")
                payload = record.get("payload") or {}
                if record.get("type") == "turn_context":
                    if stamp:
                        turn_times.append(stamp)
                    model = payload.get("model") or model
                    effort = payload.get("effort") or effort
                elif payload.get("type") == "token_count":
                    last_token_at = stamp or last_token_at
    except (OSError, ValueError):
        pass
    return {
        "available": True,
        "role": "root",
        "session_id": str(meta.get("id") or "")[:36],
        "model": model or "unknown",
        "effort": effort or "unknown",
        "last_turn_at": turn_times[-1] if turn_times else None,
        "last_token_event_at": last_token_at,
        "turns_in_recent_log_tail": len(turn_times),
        "note": "Codex turn timestamps are session activity; Git commits are independent checkpoints. No per-commit model attribution.",
    }


class WorkHistory:
    def __init__(self, root: Path, *, codex_home: Path | None = None):
        self.root = Path(root).resolve()
        self.codex_home = codex_home
        self._lock = threading.RLock()
        # Each history source has its own TTL; switching tabs does not reuse stale rows.
        self._cached_snapshots: dict[str, tuple[float, dict[str, Any]]] = {}
        self._session_cached_at: float = 0.0
        self._session_cached: dict[str, Any] | None = None

    def snapshot(self, *, limit: int = 50, force: bool = False,
                 source: str = "checkout") -> dict[str, Any]:
        """Switch between checkout history, all known refs, and published main.

        All-refs is a local/cache view and may include unpushed work. GitHub main
        uses its API and does not classify changed paths it has not inspected.
        """
        if not 1 <= limit <= MAX_COMMITS:
            raise ValueError("Limit must be 1 to 100")
        if source not in {"checkout", "all", "github"}:
            raise ValueError("Unknown history source")
        current = time.monotonic()
        refresh_seconds = 45 if source == "github" else REFRESH_SECONDS
        with self._lock:
            cached = self._cached_snapshots.get(source)
            if force or cached is None or current - cached[0] >= refresh_seconds:
                if source == "github":
                    commits = fetch_github_main(limit=MAX_COMMITS)
                    branch = "main"
                    source_name = "GITHUB_MAIN_PUBLISHED"
                    explanation = (
                        "Commits published on GitHub main. Feature-branch pushes only "
                        "appear after merging. Changed paths were not inspected."
                    )
                else:
                    extra = ("--all", "--date-order") if source == "all" else ()
                    raw = run_git(
                        self.root, "log", *extra, f"-{MAX_COMMITS}",
                        "--no-renames", "--numstat",
                        "--format=__MJH__%H%x1f%cI%x1f%an%x1f%s",
                    )
                    commits = parse_git_log(raw)
                    branch = ("All local + cached remote refs" if source == "all"
                              else run_git(self.root, "branch", "--show-current").strip())
                    source_name = ("LOCAL_ALL_KNOWN_REFS" if source == "all"
                                   else "LOCAL_GIT_COMMIT_HISTORY")
                    explanation = (
                        "All commits reachable from local branches and cached origin/* refs, "
                        "including unpushed local work. Cached remote refs can be stale."
                        if source == "all" else
                        "Commits reachable from the currently checked-out branch only."
                    )
                head = commits[0]["sha"] if commits else (
                    None if source == "github" else
                    run_git(self.root, "rev-parse", "HEAD").strip()
                )
                counts = Counter(row["category"] for row in commits)
                classified = source != "github"
                snapshot = {
                    "available": True,
                    "source": source_name, "history_source": source,
                    "source_description": explanation,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "branch": branch, "head": head,
                    "sample_size": len(commits), "categories": dict(counts),
                    "classification_available": classified,
                    "code_commits": (
                        sum(v for k, v in counts.items() if
                            k in {"CORE_FEATURE", "PRODUCT_FEATURE", "BUGFIX", "PRODUCT_CHANGE"})
                        if classified else None
                    ),
                    "change_total": (
                        sum(c["added"] + c["deleted"] for c in commits)
                        if classified else None
                    ),
                    "commits": commits, "refresh_seconds": refresh_seconds,
                    "classification_caveat": (
                        "File-based labels and author identity do not prove acceptance "
                        "or model attribution." if classified else
                        "GitHub metadata only: no changed-file categories or line counts were inspected."
                    ),
                }
                self._cached_snapshots[source] = (time.monotonic(), snapshot)
            if force or self._session_cached is None or current - self._session_cached_at >= 25:
                self._session_cached = session_activity(self.root, codex_home=self.codex_home)
                self._session_cached_at = time.monotonic()
            result = dict(self._cached_snapshots[source][1])
            result["codex"] = dict(self._session_cached or {})
            result["commits"] = result["commits"][:limit]
            return result
