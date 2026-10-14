"""Read-only, source-attributed MJobs delivery timeline.

Combines every reachable local Git commit, GitHub PR/issue/branch metadata,
original MJ-REQ/MJ-REF changes committed on first-parent main, local workspaces,
and captured acceptance deltas. No fetch, checkout, stage, approval, model call,
or modification of product acceptance is performed.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any
from urllib.parse import quote

from .progress_history import ProgressHistory

REPO = "MulattoTech/MJobs"
SHA = re.compile(r"^[a-f0-9]{40}$", re.I)
REQ = re.compile(r"MJ-(?:REQ|REF)-\d{3}(?:\.[A-Za-z0-9_-]+)?", re.I)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_GIT_COMMITS = 10000
MAX_MATRIX_REVISIONS = 1000
MAX_GH_PAGES = 20
MAX_WORKSPACE_SCANS = 130


def _run(arguments: list[str], *, timeout: float = 25) -> str:
    try:
        done = subprocess.run(
            arguments, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Read-only history source unavailable") from exc
    if done.returncode:
        raise RuntimeError(f"Read-only source exited {done.returncode}")
    return done.stdout


def _git(root: Path, *args: str, timeout: float = 25) -> str:
    return _run(["git", "--no-optional-locks", "-C", str(root), *args], timeout=timeout)


def _stamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _event(kind: str, eid: str, at: str | None, title: str, source: str,
           **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "id": eid, "at": _stamp(at), "title": title[:260],
            "source": source, **extra}


def _git_rows(root: Path) -> list[dict[str, Any]]:
    output = _git(root, "log", "--all", "--date-order", "--no-patch",
                  f"--max-count={MAX_GIT_COMMITS + 1}",
                  "--format=%H%x1f%cI%x1f%an%x1f%s", timeout=30)
    rows = []
    for line in output.splitlines():
        parts = line.split("\x1f", 3)
        if len(parts) != 4 or not SHA.fullmatch(parts[0]):
            continue
        sha, at, author, title = parts
        rows.append({"sha": sha.lower(), "at": at, "author": author[:100],
                     "title": title[:260]})
    return rows


def _git_matrix_commits(root: Path, matrix_path: str) -> list[dict[str, str]]:
    output = _git(
        root, "log", "origin/main", "--first-parent",
        "--diff-merges=first-parent", "--no-patch",
        f"--max-count={MAX_MATRIX_REVISIONS + 1}",
        "--format=%H%x1f%cI%x1f%s", "--", matrix_path, timeout=25,
    )
    result = []
    for line in output.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) == 3 and SHA.fullmatch(parts[0]):
            result.append({"sha": parts[0].lower(), "at": parts[1], "subject": parts[2]})
    return result


def _batch_matrix(root: Path, commits: list[dict[str, str]],
                  matrix_path: str) -> list[dict[str, str] | None]:
    if not commits:
        return []
    requests = [f"{item['sha']}:{matrix_path}" for item in commits]
    try:
        proc = subprocess.Popen(
            ["git", "--no-optional-locks", "-C", str(root), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW,
        )
        raw, _ = proc.communicate(
            ("\n".join(requests) + "\n").encode("ascii"), timeout=65,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if "proc" in locals():
            proc.kill()
        raise RuntimeError("Matrix history cannot be read") from exc
    if proc.returncode:
        raise RuntimeError("Matrix history object batch failed")
    offset = 0
    records = []
    for _ in requests:
        end = raw.find(b"\n", offset)
        if end < 0:
            raise RuntimeError("Truncated Git object batch")
        head = raw[offset:end].split()
        offset = end + 1
        if len(head) >= 2 and head[-1] == b"missing":
            records.append(None)
            continue
        if len(head) != 3 or head[1] != b"blob":
            raise RuntimeError("Unexpected Git object in matrix batch")
        length = int(head[2])
        if length < 0 or length > 6_000_000 or len(raw) < offset + length:
            raise RuntimeError("Matrix JSON object size is invalid")
        try:
            document = json.loads(raw[offset:offset + length])
        except (ValueError, UnicodeDecodeError):
            document = None
        offset += length + 1
        records.append(document if isinstance(document, dict) else None)
    return records


def _status_map(matrix: dict | None, name: str) -> dict[str, str]:
    if not matrix:
        return {}
    if name == "original":
        rows = matrix.get("requirements") or []
        if not isinstance(rows, list):
            return {}
        return {str(r["requirement_id"]): str(r.get("result")) for r in rows
                if isinstance(r, dict) and r.get("requirement_id")
                and r.get("result") in ("PASS", "PARTIAL", "NOT_RUN", "FAIL", "BLOCKED")}
    values = matrix.get("result_by_id") or {}
    if not isinstance(values, dict):
        return {}
    return {str(rid): str(value.get("result") if isinstance(value, dict) else value)
            for rid, value in values.items()
            if (value.get("result") if isinstance(value, dict) else value)
            in ("PASS", "PARTIAL", "NOT_RUN", "FAIL", "BLOCKED")}


def _issue_seeds(root: Path) -> dict[str, int]:
    path = root / "planning" / "ISSUE_SEED.jsonl"
    result = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf8").splitlines():
        try:
            obj = json.loads(line)
            issue = obj.get("issue_number") or obj.get("issue")
            if obj.get("id") and isinstance(issue, int) and issue > 0:
                result[obj["id"]] = issue
        except (ValueError, TypeError, AttributeError):
            pass
    return result


def _matrix_events(root: Path, seeds: dict[str, int]) -> tuple[list[dict], dict]:
    events: list[dict] = []
    coverage = {}
    for matrix_path, kind in (("planning/CAPABILITY_MATRIX.json", "original"),
                              ("planning/REFERENCE_GAP_MATRIX.json", "reference")):
        revisions = _git_matrix_commits(root, matrix_path)
        truncated = len(revisions) > MAX_MATRIX_REVISIONS
        revisions = revisions[:MAX_MATRIX_REVISIONS]
        documents = _batch_matrix(root, revisions, matrix_path)
        # Each first-parent revision is authoritative for its main-branch
        # state. The oldest revision is a baseline, not a promotion.
        older = None
        comparisons = 0
        for revision, document in reversed(list(zip(revisions, documents))):
            current = _status_map(document, kind)
            if not current:
                older = None
                continue
            if older is not None:
                comparisons += 1
                for rid in sorted(set(older) & set(current)):
                    before, after = older[rid], current[rid]
                    if before == after:
                        continue
                    issue = seeds.get(rid)
                    events.append(_event(
                        "acceptance", f"matrix:{revision['sha']}:{rid}",
                        revision["at"], f"{rid}: {before} -> {after}",
                        "Git main acceptance matrix",
                        requirement_id=rid, before=before, after=after,
                        verified_pass=(after == "PASS"),
                        sha=revision["sha"], priority=None,
                        url=(f"https://github.com/{REPO}/issues/{issue}" if issue else None),
                        evidence=f"Git main matrix revision {revision['sha'][:12]}",
                    ))
            older = current
        coverage[kind] = {
            "revisions_examined": len(revisions),
            "compared": comparisons,
            "truncated": truncated,
            "note": "Changes recorded on first-parent published main; this is not the date work began.",
        }
    return events, coverage


def _captured_transitions(history: ProgressHistory, seeds: dict[str, int]) -> list[dict]:
    with history._lock, history._db() as db:
        rows = db.execute(
            "SELECT id,captured_at,data FROM observations ORDER BY id ASC"
        ).fetchall()
    previous = None
    events = []
    for snapshot_id, stamp, body in rows:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            previous = None
            continue
        if previous:
            for field in ("requirements", "references"):
                old, current = previous.get(field) or {}, data.get(field) or {}
                for rid in sorted(set(old) & set(current)):
                    from_state = (old[rid] or {}).get("status")
                    to_state = (current[rid] or {}).get("status")
                    if not from_state or not to_state or from_state == to_state:
                        continue
                    issue = seeds.get(rid)
                    events.append(_event(
                        "capture", f"capture:{snapshot_id}:{rid}", stamp,
                        f"{rid}: {from_state} -> {to_state}", "Local acceptance capture",
                        requirement_id=rid, before=from_state, after=to_state,
                        verified_pass=(to_state == "PASS"),
                        url=f"https://github.com/{REPO}/issues/{issue}" if issue else None,
                        evidence=f"First observed in capture #{snapshot_id}; exact change time unknown",
                    ))
        previous = data
    return events


def _gh_pages(endpoint: str) -> list[dict]:
    rows = []
    for page in range(1, MAX_GH_PAGES + 1):
        address = f"repos/{REPO}/{endpoint}&per_page=100&page={page}"
        data = json.loads(_run(["gh", "api", address], timeout=22))
        if not isinstance(data, list):
            raise RuntimeError("GitHub collection was not a list")
        rows.extend(x for x in data if isinstance(x, dict))
        if len(data) < 100:
            break
    return rows


def _github_events() -> tuple[list[dict], dict]:
    events = []
    sources = {}
    for family, endpoint in (
        ("pull_requests", "pulls?state=all&sort=updated&direction=desc"),
        ("issues", "issues?state=all&sort=updated&direction=desc"),
        ("branches", "branches?protected=false"),
    ):
        try:
            rows = _gh_pages(endpoint)
        except (RuntimeError, ValueError, TypeError) as exc:
            sources[family] = {"available": False, "reason": str(exc)[:120]}
            continue
        sources[family] = {
            "available": True, "records": len(rows),
            "may_be_truncated": len(rows) >= MAX_GH_PAGES * 100,
        }
        if family == "pull_requests":
            for row in rows:
                number = row.get("number")
                if not isinstance(number, int):
                    continue
                url = f"https://github.com/{REPO}/pull/{number}"
                title = str(row.get("title") or f"PR #{number}")[:240]
                events.append(_event(
                    "pr", f"pr:opened:{number}", row.get("created_at"),
                    f"PR #{number} opened: {title}", "GitHub pull requests",
                    number=number, url=url, status="opened",
                ))
                completed = row.get("merged_at") or row.get("closed_at")
                if completed:
                    merged = bool(row.get("merged_at"))
                    events.append(_event(
                        "pr", f"pr:{'merged' if merged else 'closed'}:{number}",
                        completed, f"PR #{number} {'merged' if merged else 'closed without merge'}: {title}",
                        "GitHub pull requests", number=number, url=url,
                        status="merged" if merged else "closed",
                    ))
        elif family == "issues":
            for row in rows:
                if "pull_request" in row:
                    continue
                number = row.get("number")
                if not isinstance(number, int):
                    continue
                url = f"https://github.com/{REPO}/issues/{number}"
                title = str(row.get("title") or f"Issue #{number}")[:240]
                events.append(_event(
                    "issue", f"issue:created:{number}", row.get("created_at"),
                    f"Issue #{number} created: {title}", "GitHub issues",
                    number=number, url=url, status="opened",
                ))
                if row.get("closed_at"):
                    events.append(_event(
                        "issue", f"issue:closed:{number}", row["closed_at"],
                        f"Issue #{number} closed: {title}", "GitHub issues",
                        number=number, url=url, status="closed",
                    ))
        else:
            sources["branches"]["heads"] = [
                {"name": str(row.get("name") or "")[:160],
                 "sha": str((row.get("commit") or {}).get("sha") or "")[:40]}
                for row in rows if row.get("name")
            ]
    return events, sources


def _remote_branch_events(heads: list[dict]) -> tuple[list[dict], dict]:
    """Read exact GitHub branch-only commits and head revisions, never fetch locally."""
    if not heads:
        return [], {"inspected": 0, "additional_commit_rows": 0,
                    "truncated": False, "errors": []}

    def inspect(row: dict) -> tuple[list[dict], dict]:
        name = str(row.get("name") or "")
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", name) or ".." in name:
            return [], {"name": name[:60], "error": "Invalid branch name"}
        address = f"repos/{REPO}/compare/main...{quote(name, safe='')}"
        try:
            compare = json.loads(_run(["gh", "api", address], timeout=25))
        except (RuntimeError, ValueError, TypeError):
            return [], {"name": name, "error": "Branch comparison unavailable"}
        if not isinstance(compare, dict):
            return [], {"name": name, "error": "Malformed GitHub comparison"}
        commits = compare.get("commits") or []
        if not isinstance(commits, list):
            return [], {"name": name, "error": "Missing commit list"}
        ahead = int(compare.get("ahead_by") or 0)
        response = []
        for entry in commits:
            sha = entry.get("sha")
            metadata = entry.get("commit") or {}
            if not isinstance(sha, str) or not SHA.fullmatch(sha):
                continue
            author = metadata.get("author") or {}
            committer = metadata.get("committer") or {}
            when = committer.get("date") or author.get("date")
            subject = str(metadata.get("message") or "(untitled commit)").splitlines()[0]
            response.append(_event(
                "commit", f"commit:{sha.lower()}", when,
                subject, "GitHub remote branch",
                sha=sha.lower(), author=str(author.get("name") or "Unknown")[:100],
                scopes=[f"GitHub branch: {name}"],
                requirement_ids=REQ.findall(subject),
                url=f"https://github.com/{REPO}/commit/{sha.lower()}",
                published_on_cached_main=False,
            ))
        head = str(row.get("sha") or "")
        selected = next((e for e in response if e.get("sha") == head), None)
        if selected:
            response.append(_event(
                "branch", f"branch-head:{name}", selected["at"],
                f"GitHub branch HEAD: {name} @ {head[:12]}",
                "GitHub branch head", sha=head, branch=name,
                url=f"https://github.com/{REPO}/tree/{quote(name, safe='/')}",
                evidence="Last commit timestamp on branch, not the branch creation date",
            ))
        return response, {
            "name": name, "ahead_by": ahead,
            "returned": len(commits), "truncated": ahead > len(commits),
        }

    # Avoid serial 25s GH calls for branches that are inaccessible or stale.
    with ThreadPoolExecutor(max_workers=5) as pool:
        groups = list(pool.map(inspect, heads[:120]))
    found = []
    incomplete = []
    for entries, meta in groups:
        found.extend(entries)
        if meta.get("error") or meta.get("truncated"):
            incomplete.append(meta)
    return found, {
        "inspected": len(groups),
        "additional_commit_rows": sum(1 for e in found if e["kind"] == "commit"),
        "branch_head_events": sum(1 for e in found if e["kind"] == "branch"),
        "truncated": bool(incomplete) or len(heads) > 120,
        "errors": incomplete[:20],
    }


def _workspace_inventory(root: Path) -> tuple[list[dict], list[dict], dict]:
    records = []
    events = []
    checked = 0
    archived_dirty = 0
    try:
        lines = _git(root, "worktree", "list", "--porcelain").splitlines()
    except RuntimeError:
        lines = []
    for line in lines:
        if line.startswith("worktree "):
            records.append({"workspace": line[len("worktree "):], "kind": "registered"})
        elif line.startswith("branch ") and records:
            records[-1]["branch"] = line[len("branch refs/heads/"):] if line.startswith("branch refs/heads/") else line[7:]
    independents = []
    base = root / ".mjobs-recovery"
    for subdir in ("active", "clones"):
        folder = base / subdir
        if folder.is_dir():
            independents.extend(
                p for p in sorted(folder.iterdir()) if p.is_dir() and (p / ".git").is_dir()
            )
    for path in independents:
        records.append({"workspace": str(path), "kind": "independent clone"})
    dedup = list({x["workspace"].casefold(): x for x in records}.values())
    # All locations are inventoried. Verify dirty status for current/active
    # workspaces plus as many other registered workspaces as bounded.
    active = (root / ".mjobs-recovery" / "active").resolve()
    ordered = sorted(dedup, key=lambda row: (
        0 if Path(row["workspace"]).resolve() == root.resolve() else
        1 if Path(row["workspace"]).resolve().is_relative_to(active) else 2,
        row["workspace"].casefold()))
    for row in ordered[:MAX_WORKSPACE_SCANS]:
        path = Path(row["workspace"])
        if not path.is_dir():
            row["state"] = "MISSING"
            continue
        try:
            output = _git(path, "status", "--porcelain=v1", "--untracked-files=normal", timeout=4)
        except RuntimeError:
            row["state"] = "UNAVAILABLE"
            continue
        checked += 1
        changes = [
            line for line in output.splitlines()
            if len(line) > 3 and not line[3:].replace("\\", "/").casefold().startswith(
                (".mjobs-recovery/", ".worktrees/", ".mjobs-run/"))
        ]
        row["state"] = "DIRTY" if changes else "CLEAN"
        row["changed_count"] = len(changes)
        if changes:
            # Historical clone/worktree dirtiness is captured in the full
            # inventory; it is NOT a new delivery event on the present date.
            # Only active checkouts get a time-of-observation timeline row.
            resolved = path.resolve()
            if resolved == root.resolve() or resolved.is_relative_to(active):
                events.append(_event(
                    "workspace", f"workspace:{str(path).casefold()}",
                    datetime.now(timezone.utc).isoformat(),
                    f"{len(changes)} uncommitted file(s): {path.name}",
                    "Local working-copy observation",
                    workspace=str(path), changed_count=len(changes),
                    paths=[line[3:][:220] for line in changes[:20] if len(line) > 3],
                    evidence="Observed now; modification time and feature acceptance are NOT inferred",
                ))
            else:
                archived_dirty += 1
    return ordered, events, {
        "registered_and_independent": len(dedup),
        "statuses_inspected": checked,
        "archived_dirty_workspaces": archived_dirty,
        "inspection_limit": MAX_WORKSPACE_SCANS,
        "not_inspected": max(0, len(dedup) - MAX_WORKSPACE_SCANS),
    }


def _all_git_events(root: Path) -> tuple[list[dict], dict]:
    # Hash-based deduplication across all known refs plus independent backups.
    observed: dict[str, dict] = {}
    groups: dict[str, int] = {}
    locations = [("Canonical Git refs", root)]
    archive = root / ".mjobs-recovery"
    for kind in ("active", "clones"):
        folder = archive / kind
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.is_dir() and (path / ".git").is_dir():
                    locations.append((f"Local {kind}: {path.name}", path))
    for label, cwd in locations:
        try:
            rows = _git_rows(cwd)
        except RuntimeError:
            groups[label] = -1
            continue
        groups[label] = len(rows)
        for row in rows:
            sha = row["sha"]
            item = observed.get(sha)
            if not item:
                item = _event(
                    "commit", f"commit:{sha}", row["at"], row["title"],
                    "Git history", sha=sha, author=row["author"],
                    scopes=[], requirement_ids=REQ.findall(row["title"]),
                )
                observed[sha] = item
            if label not in item["scopes"]:
                item["scopes"].append(label)
    published = set()
    try:
        published = set(_git(
            root, "rev-list", "origin/main", "--max-count=10000",
        ).splitlines())
    except RuntimeError:
        pass
    for item in observed.values():
        item["published_on_cached_main"] = item["sha"] in published
        item["url"] = (f"https://github.com/{REPO}/commit/{item['sha']}"
                       if item["sha"] in published else None)
    return list(observed.values()), {
        "unique_commits": len(observed),
        "scopes": groups,
        "truncation_possible": any(n >= MAX_GIT_COMMITS for n in groups.values()),
    }


class DeliveryLedger:
    """Immutable source snapshots cached briefly; UI offsets do not change source state."""

    def __init__(self, root: Path, progress_history: ProgressHistory):
        self.root = Path(root).resolve()
        self.progress_history = progress_history
        self._lock = threading.RLock()
        self._cached_at = 0.0
        self._cache: dict[str, Any] | None = None

    def _read(self) -> dict[str, Any]:
        seeds = _issue_seeds(self.root)
        coverage: dict[str, Any] = {}
        events = []
        for name, collector in (
            ("git", lambda: _all_git_events(self.root)),
            ("matrix", lambda: _matrix_events(self.root, seeds)),
            ("workspaces", lambda: _workspace_inventory(self.root)),
        ):
            try:
                if name == "workspaces":
                    inventory, found, info = collector()
                    coverage[name] = info
                else:
                    found, info = collector()
                    coverage[name] = info
                events.extend(found)
            except (RuntimeError, OSError, ValueError, TypeError) as exc:
                coverage[name] = {"available": False, "reason": str(exc)[:140]}
                if name == "workspaces":
                    inventory = []
        try:
            events.extend(_captured_transitions(self.progress_history, seeds))
            coverage["captures"] = {"available": True}
        except (OSError, ValueError, TypeError) as exc:
            coverage["captures"] = {"available": False, "reason": str(exc)[:140]}
        github_events, github_sources = _github_events()
        events.extend(github_events)
        # Remote GitHub branches may have unique commits not present in locally
        # cached Git refs. Compare their ancestry without fetching or writing.
        remote_events, branch_coverage = _remote_branch_events(
            (github_sources.get("branches") or {}).get("heads") or []
        )
        events.extend(remote_events)
        github_sources["branch_comparisons"] = branch_coverage
        coverage["github"] = github_sources
        events = [e for e in events if e["at"]]
        events.sort(key=lambda e: (e["at"], e["id"]), reverse=True)
        seen = {}
        unique = []
        for event in events:
            prior = seen.get(event["id"])
            if prior:
                if event["kind"] == "commit":
                    prior["scopes"] = list(dict.fromkeys(
                        (prior.get("scopes") or []) + (event.get("scopes") or [])))
                    if not prior.get("url") and event.get("url"):
                        prior["url"] = event["url"]
                continue
            seen[event["id"]] = event
            unique.append(event)
        return {
            "events": unique,
            "inventory": inventory,
            "coverage": coverage,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "note": (
                "Git history is keyed by SHA; worktree edits are snapshots, not commits. "
                "PR/issue closures do not automatically prove MJ-REQ PASS. "
                "Historical matrix transitions are verified first-parent main changes; "
                "acceptance snapshots establish only when a transition was first observed. "
                "GitHub and Git ref inventories are limited to authenticated/discoverable sources."
            ),
        }

    def query(self, *, offset: int = 0, limit: int = 60,
              kind: str = "all", search: str = "", refresh: bool = False) -> dict[str, Any]:
        if not 0 <= offset <= 100_000 or not 1 <= limit <= 150:
            raise ValueError("Invalid history pagination")
        if kind not in {"all", "commit", "pr", "issue", "acceptance", "capture", "workspace", "branch", "pass"}:
            raise ValueError("Unknown activity source filter")
        if len(search) > 120:
            raise ValueError("Activity search too long")
        with self._lock:
            if refresh or self._cache is None or time.monotonic() - self._cached_at >= 100:
                self._cache = self._read()
                self._cached_at = time.monotonic()
            result = self._cache
        events = result["events"]
        if kind == "pass":
            events = [e for e in events if e["kind"] in {"acceptance", "capture"}
                      and e.get("verified_pass")]
        elif kind != "all":
            events = [e for e in events if e["kind"] == kind]
        if search.strip():
            term = search.strip().casefold()
            events = [e for e in events if term in e["title"].casefold()
                      or term in str(e.get("requirement_id") or "").casefold()
                      or term in str(e.get("sha") or "").casefold()
                      or term in str(e.get("workspace") or "").casefold()]
        counts = dict(Counter(row["kind"] for row in result["events"]))
        return {
            "available": True, "events": events[offset:offset + limit],
            "total_matching": len(events), "total_recorded": len(result["events"]),
            "offset": offset, "limit": limit,
            "has_more": offset + limit < len(events), "kind": kind, "search": search,
            "counts": counts, "coverage": result["coverage"],
            "workspaces": result["inventory"],
            "observed_at": result["observed_at"], "note": result["note"],
        }
