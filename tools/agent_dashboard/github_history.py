"""Read-only GitHub main commit metadata for MJobs Mission Control.

GitHub main is fundamentally different from the checkout HEAD or local Git's
cached remote refs. This uses the authenticated GitHub CLI and never fetches,
checks out, updates refs, or starts an AI agent.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

REPO = "MulattoTech/MJobs"
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
ISSUE_RE = re.compile(r"(?:#\d{1,6}|MJ-(?:REQ|REF)-\d{3}(?:\.[A-Za-z0-9_-]+)?)", re.I)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def parse_main_commits(rows: Any) -> list[dict[str, Any]]:
    """Normalize bounded GitHub REST commit rows without inventing file stats."""
    if not isinstance(rows, list):
        raise ValueError("GitHub returned an invalid commits payload")
    result = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        sha = row.get("sha")
        commit = row.get("commit")
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha) or not isinstance(commit, dict):
            continue
        msg = commit.get("message")
        if not isinstance(msg, str):
            msg = ""
        subject = msg.splitlines()[0][:260] if msg else "(no subject)"
        committer = commit.get("committer") or {}
        author = commit.get("author") or {}
        stamp = committer.get("date") or author.get("date")
        if not isinstance(stamp, str):
            stamp = None
        who = author.get("name") or committer.get("name") or "Unknown"
        result.append({
            "sha": sha.lower(), "short_sha": sha[:10].lower(),
            "timestamp": stamp, "author": str(who)[:120], "subject": subject,
            "category": "OTHER",
            "category_explanation": "Published GitHub main commit; changed files not inspected",
            "area": "GitHub main",
            "issue_refs": list(dict.fromkeys(ISSUE_RE.findall(subject)))[:8],
            "files": [], "files_changed": None, "files_truncated": 0,
            "added": None, "deleted": None, "production_lines_changed": None,
            "metadata_only": True,
            "published": True,
        })
    return result


def fetch_github_main(*, limit: int = 100) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100:
        raise ValueError("GitHub commit limit must be 1 to 100")
    try:
        completed = subprocess.run(
            ["gh", "api", f"repos/{REPO}/commits?sha=main&per_page={limit}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=18, creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("GitHub main commit feed is unavailable") from exc
    if completed.returncode:
        # Never display CLI stderr: it could contain account-specific metadata.
        raise RuntimeError("GitHub main commit feed is unavailable")
    try:
        rows = json.loads(completed.stdout)
        if not isinstance(rows, list):
            raise ValueError("Expected GitHub commit list")
    except (ValueError, TypeError) as exc:
        raise RuntimeError("GitHub returned malformed commit metadata") from exc
    return parse_main_commits(rows)
