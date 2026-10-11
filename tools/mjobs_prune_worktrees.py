"""Conservatively remove clean obsolete MJobs-linked worktrees.

No --force, reset, stash or git clean. Never touches the canonical checkout,
dirty/untracked/ignored files, or paths outside the MJobs internal worktree areas.
Run after verifying an all-refs Git bundle has been saved.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SAFE_ROOTS = [ROOT / ".worktrees", ROOT / ".git" / "mjobs-reconciliation"]


def git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *map(str, args)], text=True,
                          encoding="utf-8", errors="replace", capture_output=True,
                          timeout=timeout, check=False)


def registered() -> list[dict]:
    result = git("worktree", "list", "--porcelain")
    if result.returncode:
        raise RuntimeError(result.stderr)
    out = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            out.append({"path": line[9:]})
        elif line.startswith("branch ") and out:
            out[-1]["branch"] = line[7:]
        elif line.startswith("HEAD ") and out:
            out[-1]["sha"] = line[5:]
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="0: all eligible")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    if not args.bundle.is_file() or args.bundle.stat().st_size < 1024:
        raise RuntimeError("Missing non-empty Git backup bundle")
    verify = git("bundle", "verify", str(args.bundle), timeout=60)
    if verify.returncode != 0:
        raise RuntimeError("Backup bundle validation failed")
    canonical = ROOT.resolve()
    roots = [p.resolve() for p in SAFE_ROOTS]
    entries = registered()
    counts = Counter()
    examples = {}
    completed = []
    failed = []
    for number, item in enumerate(entries, 1):
        target = Path(item["path"]).resolve()
        if target == canonical:
            counts["canonical-preserved"] += 1
            continue
        if not any(target.is_relative_to(base) for base in roots):
            counts["outside-internal-root-preserved"] += 1
            examples.setdefault("outside-internal-root-preserved", str(target))
            continue
        if not target.exists():
            counts["missing-path-prune-candidate"] += 1
            continue
        # This includes ignored / untracked artifacts, which Git alone might
        # overlook and which can be personally valuable.
        status = subprocess.run(
            ["git", "-C", str(target), "status", "--porcelain",
             "--ignored=matching", "--untracked-files=all"],
            text=True, encoding="utf-8", errors="replace", capture_output=True,
            timeout=18, check=False)
        if status.returncode != 0:
            counts["unreadable-preserved"] += 1
            examples.setdefault("unreadable-preserved", str(target))
            continue
        if status.stdout.strip():
            counts["dirty-or-ignored-preserved"] += 1
            examples.setdefault("dirty-or-ignored-preserved", str(target))
            continue
        counts["clean-internal-worktree"] += 1
        if args.apply:
            removal = git("worktree", "remove", str(target), timeout=70)
            if removal.returncode:
                failed.append({"path": str(target), "error": removal.stderr[:300]})
            else:
                completed.append(str(target))
                if len(completed) % 40 == 0:
                    print(f"Removed {len(completed)} clean worktrees...", flush=True)
                if args.limit and len(completed) >= args.limit:
                    break
    summary = {"mode": "apply" if args.apply else "dry-run",
               "total_registered_at_start": len(entries), "counts": dict(counts),
               "examples": examples, "removed": len(completed),
               "failed": failed[:20], "remaining_registered": len(registered())}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
