"""Mirror ONLY a local Codex sprint commit reference into canonical MJobs Git Graph.

Never merges, pushes, stages, cherry-picks or changes the canonical checkout.
Uncommitted clone modifications are intentionally NOT Git commits.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
META = ROOT / ".git" / "mjobs-p0p1-round3"
CHECKPOINT = META / "latest.json"
MIRROR = "refs/heads/mjobs/sprint-live"
STATE = META / "sprint-graph-mirror.json"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, encoding="utf-8", errors="replace", text=True,
        timeout=35, check=False,
    )
    if check and completed.returncode:
        raise RuntimeError(f"Git exited {completed.returncode}: {completed.stderr[-350:]}")
    return completed.stdout.strip() if completed.returncode == 0 else ""


def synced() -> dict:
    if not CHECKPOINT.is_file():
        return {"status": "NO_CAMPAIGN"}
    row = json.loads(CHECKPOINT.read_text(encoding="utf-8-sig"))
    workspace = Path(row.get("worktree") or "").resolve(strict=True)
    inside = (ROOT / ".mjobs-recovery" / "active").resolve()
    if not workspace.is_relative_to(inside) or workspace == inside or not (workspace / ".git").is_dir():
        raise RuntimeError("Unexpected external sprint checkout: refusing to mirror")
    source_branch = git(workspace, "branch", "--show-current")
    if not source_branch:
        raise RuntimeError("Sprint branch is detached")
    git(workspace, "check-ref-format", "--branch", source_branch)
    source_head = git(workspace, "rev-parse", "HEAD")
    prior = git(ROOT, "rev-parse", "--verify", MIRROR, check=False)
    if prior and not STATE.exists():
        raise RuntimeError(f"Existing {MIRROR} is not managed by this tool; refusing to overwrite")
    if source_head != prior:
        git(ROOT, "fetch", "--no-tags", "--no-write-fetch-head", str(workspace),
            f"+refs/heads/{source_branch}:{MIRROR}")
    now = git(ROOT, "rev-parse", "--verify", MIRROR)
    if now != source_head:
        raise RuntimeError("Mirror SHA did not match the inspected source")
    details = {
        "status": "MIRRORED" if prior != now else "UNCHANGED",
        "utc": datetime.now(timezone.utc).isoformat(),
        "source_workspace": str(workspace),
        "source_branch": source_branch,
        "mirror_branch": "mjobs/sprint-live",
        "head": now,
        "new_commits_ahead_of_main": int(
            git(ROOT, "rev-list", "--count", f"origin/main..{MIRROR}")
        ),
        "note": "Local view only; no merge or push. Dirty files appear in Mission Control, not Git Graph.",
    }
    META.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".partial")
    tmp.write_text(json.dumps(details, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)
    return details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if not args.watch:
        print(json.dumps(synced(), indent=2))
        return
    import msvcrt
    META.mkdir(parents=True, exist_ok=True)
    lock = META / "sprint-graph-watch.lock"
    with lock.open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("Another Git Graph mirror watcher is already running")
        print("WATCHING_MJOBS_SPRINT_GRAPH", flush=True)
        try:
            while True:
                try:
                    info = synced()
                    print(info["utc"], info["status"], info.get("head", "")[:12],
                          "ahead", info.get("new_commits_ahead_of_main", 0), flush=True)
                except Exception as exc:
                    print("MIRROR_WARNING", str(exc)[:240], flush=True)
                time.sleep(30)
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


if __name__ == "__main__":
    main()
