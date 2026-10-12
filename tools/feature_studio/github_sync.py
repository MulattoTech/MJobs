"""GitHub integration primitives for Feature Studio.

All GitHub writes require an explicit method call and happen on a new branch with a
draft PR. No code path merges, modifies main directly, or installs GitHub Apps.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .control import REPO_NAME, command


def verify_webhook(payload: bytes, signature: str | None, secret: bytes) -> bool:
    if not secret or len(payload) > 1_048_576:
        return False
    if not signature or not re.fullmatch(r"sha256=[a-f0-9]{64}", signature):
        return False
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def rollout_preview(repositories: list[dict[str, Any]], *, pilot: str = "MulattoTech/MJobs") -> dict[str, Any]:
    eligible = sorted({row["repository"] for row in repositories
                       if REPO_NAME.fullmatch(row["repository"]) and not row.get("archived", False)})
    return {
        "pilot": pilot,
        "eligible_repositories": eligible,
        "skipped_archived": sorted(row["repository"] for row in repositories if row.get("archived")),
        "requested_rollout": len(eligible),
        "executed_rollout": 0,
        "approval_required": True,
        "mode": "PREVIEW_ONLY",
    }


def rollout_repo(repo: str, *, source: Path, branch_suffix: str = "v1") -> dict[str, Any]:
    """Create a *draft* configuration PR on one explicitly selected repository.

    Requires a logged-in GitHub CLI and user-initiated invocation. Existing forms
    are preserved; a conflict fails closed. Never executes arbitrary repository code.
    """
    if not REPO_NAME.fullmatch(repo):
        raise ValueError("Invalid repository identity")
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,30}", branch_suffix):
        raise ValueError("Invalid branch suffix")
    if not REPO_NAME.fullmatch(repo):
        raise ValueError("Invalid repository identity")
    form = source.read_text(encoding="utf-8")
    if "name: Feature Studio guided intake" not in form:
        raise ValueError("Source intake template is not recognized")
    if len(form) > 300_000:
        raise ValueError("Template exceeds size limit")
    branch = f"feature-studio/install-{branch_suffix}"
    path = ".github/ISSUE_TEMPLATE/feature-studio.yml"
    # Fetch only source repository metadata; never mutate an archived repo.
    metadata = json.loads(command(["gh","api",f"repos/{repo}"]))
    if metadata.get("archived"):
        raise ValueError("Archived repository; rollout not permitted")
    if not metadata.get("permissions",{}).get("push"):
        raise ValueError("Repository write permission unavailable")
    default_branch = metadata["default_branch"]
    # Explicitly fail rather than overwriting an existing template.
    probe = subprocess.run(["gh","api",f"repos/{repo}/contents/{path}"],
                           capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=20)
    if probe.returncode == 0:
        raise ValueError("Feature Studio intake form already exists; manual reconciliation required")
    if "404" not in (probe.stderr or ""):
        raise RuntimeError("Cannot establish absence of existing template")
    # Clone to a disposable checkout; no changes to the user's existing worktrees.
    with TemporaryDirectory(prefix="feature-studio-canary-") as root:
        command(["gh","repo","clone",repo,root,"--","--depth","1"],timeout=60)
        command(["git","-C",root,"switch","-c",branch])
        target=Path(root)/path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(form,encoding="utf-8")
        command(["git","-C",root,"add","--",path])
        command(["git","-C",root,"-c","user.name=Feature Studio",
                 "-c","user.email=feature-studio@users.noreply.github.com",
                 "commit","-m","chore: propose Feature Studio guided intake"])
        command(["git","-C",root,"push","-u","origin",branch],timeout=60)
        url=command(["gh","pr","create","--repo",repo,"--head",branch,
                     "--base",default_branch,"--draft",
                     "--title","chore: install Feature Studio guided intake",
                     "--body","Installs the non-executing Feature Studio native GitHub issue form. No GitHub App permissions, workflows, agents or branch protections change. Review before merge."],timeout=30).strip()
    return {"repository":repo,"branch":branch,"draft_pr":url,"merged":False}


def safe_deployment_policy() -> dict[str, Any]:
    return {"default":"read-only","git_write_mode":"draft-pr-only",
            "main_merge":"never","agents":"manual approval only",
            "secrets":"GitHub CLI credential store; never store token in feature drafts",
            "org_rollout":"explicit canaries and per-repo opt-in; no bulk auto merge"}
