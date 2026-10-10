"""Generate a VS Code multi-root view of ALL local MJobs development sources.

Registered Git worktrees share branch objects with main. Independent clones
do not; opening those as separate workspace roots is essential to seeing
commits that have NOT been pushed to GitHub.

By default this opens main, all independent MJobs clones, and a manageable
number of most-recent worktrees. Every worktree is indexed in a local JSON
registry. --all-worktrees is explicit because there may be 900+ roots.
Only local, read-only Git commands are executed. No model calls, Git fetch,
checkout, merge, reset, commit, clean or push.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

REPO_NAME = "MulattoTech/MJobs"


def git(root: Path, *args: str, optional: bool = False) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=25, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        if optional:
            return None
        raise RuntimeError("Git is unavailable for MJobs workspace discovery")
    if result.returncode:
        if optional:
            return None
        raise RuntimeError("Cannot inspect the requested local MJobs repository")
    return result.stdout.strip()


def is_mjobs_remote(repo: Path) -> bool:
    remote = git(repo, "config", "--get", "remote.origin.url", optional=True)
    if not remote:
        return False
    normalized = remote.replace("\\", "/").strip().lower()
    return bool(re.search(r"(^|[:/])mulattotech/mjobs(?:\.git)?/?$", normalized))


def git_common_dir(repo: Path) -> Path | None:
    value = git(repo, "rev-parse", "--git-common-dir", optional=True)
    if not value:
        return None
    directory = Path(value)
    return (directory if directory.is_absolute() else repo / directory).resolve()


def linked_worktrees(repo: Path) -> list[dict]:
    result: list[dict] = []
    current: dict | None = None
    for line in (git(repo, "worktree", "list", "--porcelain") or "").splitlines():
        if line.startswith("worktree "):
            if current:
                result.append(current)
            current = {
                "path": str(Path(line[9:].strip()).resolve()),
                "head": "",
                "branch": "",
                "detached": False,
                "prunable": False,
            }
        elif current is not None:
            if line.startswith("HEAD "):
                current["head"] = line[5:]
            elif line.startswith("branch "):
                current["branch"] = line[7:]
            elif line == "detached":
                current["detached"] = True
            elif line.startswith("prunable "):
                current["prunable"] = True
    if current:
        result.append(current)
    timestamps: dict[str, int] = {}
    for line in (git(
        repo, "for-each-ref",
        "--format=%(refname)%09%(committerdate:unix)", "refs/heads",
    ) or "").splitlines():
        pieces = line.split("\t", 1)
        if len(pieces) == 2 and pieces[1].isdigit():
            timestamps[pieces[0]] = int(pieces[1])
    for entry in result:
        entry["exists"] = Path(entry["path"]).is_dir()
        entry["commit_unix"] = timestamps.get(entry["branch"], 0)
    return result


def independent_clones(repo: Path) -> list[dict]:
    independent: list[dict] = []
    primary_common = git_common_dir(repo)
    for sibling in sorted(repo.parent.glob("MJobs*"), key=lambda p: p.name.lower()):
        if not sibling.is_dir() or sibling.resolve() == repo.resolve():
            continue
        top = git(sibling, "rev-parse", "--show-toplevel", optional=True)
        if not top or Path(top).resolve() != sibling.resolve():
            continue
        if not is_mjobs_remote(sibling):
            continue
        # A linked worktree can live alongside main and have its own folder.
        # It shares the Git objects/refs and must NOT be counted as an
        # independent clone just because it is outside .worktrees.
        sibling_common = git_common_dir(sibling)
        if sibling_common is None or sibling_common == primary_common:
            continue
        head = git(sibling, "rev-parse", "HEAD", optional=True) or ""
        branch = git(sibling, "symbolic-ref", "-q", "--short", "HEAD", optional=True)
        independent.append({
            "path": str(sibling.resolve()), "branch": branch or "(detached)",
            "head": head,
        })
    preferred = ("MJobs-Codex-App", "MJobs-Antigravity-Matching",
                 "MJobs-MissionControl-Launch")
    def weight(item: dict) -> tuple[int, str]:
        name = Path(item["path"]).name
        return (
            preferred.index(name) if name in preferred else
            3 if name.startswith(("MJobs-Web", "MJobs-Worker")) else 4,
            name.lower(),
        )
    return sorted(independent, key=weight)


def vscode_executable() -> str:
    options = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            options.append(Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe")
        options.append(Path("C:/Program Files/Microsoft VS Code/Code.exe"))
    for option in options:
        if option.is_file():
            return str(option)
    executable = shutil.which("code") or shutil.which("code.cmd")
    if not executable:
        raise RuntimeError(
            "VS Code not detected; generated .code-workspace can be opened manually"
        )
    return executable


def create_workspace(
    repo: Path, output: Path, *, recent: int = 25,
    all_worktrees: bool = False, allow_huge: bool = False,
) -> dict:
    repo = repo.resolve()
    output = output.resolve()
    if not repo.is_dir():
        raise ValueError("MJobs main checkout does not exist")
    top = git(repo, "rev-parse", "--show-toplevel")
    if not top or Path(top).resolve() != repo or not is_mjobs_remote(repo):
        raise ValueError("The selected root must be the MulattoTech/MJobs checkout")
    if not output.name.lower().endswith(".generated.code-workspace"):
        raise ValueError(
            "Output name must end in .generated.code-workspace to protect edited workspaces"
        )
    if not output.parent.is_dir():
        raise ValueError("Workspace output directory is missing")

    registered = linked_worktrees(repo)
    clones = independent_clones(repo)
    eligible = [
        item for item in registered
        if item["exists"] and not item["prunable"]
        and Path(item["path"]).resolve() != repo
    ]
    eligible.sort(key=lambda item: (-item["commit_unix"], item["path"].lower()))
    if all_worktrees and len(eligible) > 150 and not allow_huge:
        raise ValueError(
            f"{len(eligible)} worktrees would overload VS Code. "
            "Add -AllowHugeWorkspace only if you deliberately want every one."
        )
    if all_worktrees:
        chosen = eligible
    else:
        # Always show these real development lanes, even if their last commit
        # isn't one of the latest N. They're linked worktrees, not clones.
        pinned_names = {"MJobs-Codex-App", "MJobs-Antigravity-Matching"}
        pinned = [x for x in eligible
                  if Path(x["path"]).name in pinned_names]
        remaining = [x for x in eligible
                     if Path(x["path"]).name not in pinned_names]
        chosen = pinned + remaining[:recent]

    folders: list[dict] = []
    selected: list[dict] = []
    seen: set[str] = set()
    def add(path: str, label: str, kind: str):
        resolved = str(Path(path).resolve())
        key = os.path.normcase(resolved).lower()
        if key in seen:
            return
        seen.add(key)
        folders.append({"path": resolved, "name": label})
        selected.append({"path": resolved, "kind": kind})

    add(str(repo), "MJobs MAIN | shared Git branches", "main")
    for clone in clones:
        name = Path(clone["path"]).name
        add(clone["path"], f"CLONE | {name} | {clone['branch']}", "independent")
    for entry in chosen:
        label = (entry["branch"].removeprefix("refs/heads/")
                 if entry["branch"] else "detached")
        add(entry["path"], f"WORKTREE | {Path(entry['path']).name} | {label}", "linked")

    # Git Graph / Source Control graph shows local refs without any push.
    workspace = {
        "folders": folders,
        "settings": {
            "git.autofetch": False,
            "git.confirmSync": True,
        },
    }
    registry = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(repo),
        "explanation": (
            "Main and its linked worktrees share Git refs: their local branch "
            "commits appear in the main repo graph. Independent clones have "
            "separate Git databases and need separate Source Control roots."
        ),
        "total_registered_worktrees": len(registered),
        "available_linked_worktrees": len(eligible),
        "selected_linked_worktrees": len(chosen),
        "independent_clone_count": len(clones),
        "selected_folders": selected,
        "independent_clones": clones,
        "all_worktrees": registered,
    }
    registry_path = output.with_name(
        output.name.replace(".generated.code-workspace", ".generated.worktrees.json")
    )
    for target, payload in ((output, workspace), (registry_path, registry)):
        temp = target.with_name(target.name + ".tmp")
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            json.loads(temp.read_text(encoding="utf-8"))
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    return {
        "workspace_file": str(output),
        "registry_file": str(registry_path),
        "folder_count": len(folders),
        "registered_worktrees": len(registered),
        "available_worktrees": len(eligible),
        "opened_linked_worktrees": len(chosen),
        "independent_clones": len(clones),
        "graph_only_worktrees": len(eligible)-len(chosen),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recent-worktrees", type=int, default=25)
    parser.add_argument("--all-worktrees", action="store_true")
    parser.add_argument("--allow-huge-workspace", action="store_true")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument("--reuse-window", action="store_true")
    parser.add_argument("--install-git-graph", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.recent_worktrees <= 250:
        parser.error("recent-worktrees must be between 0 and 250")
    try:
        summary = create_workspace(
            args.repo_root, args.output, recent=args.recent_worktrees,
            all_worktrees=args.all_worktrees,
            allow_huge=args.allow_huge_workspace,
        )
        for key, value in summary.items():
            print(f"{key.upper()}={value}", flush=True)
        print("All registered worktrees are inventoried, and all independent clones are workspace roots.")
        print("Shared worktree branches are visible from main's local Git graph without any GitHub push.")
        if not args.no_launch:
            vscode = vscode_executable()
            command = [vscode, "--reuse-window" if args.reuse_window else "--new-window",
                       summary["workspace_file"]]
            subprocess.Popen(
                command, cwd=str(args.repo_root.resolve()),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("LAUNCHED_VSCODE=" + vscode, flush=True)
            if args.install_git_graph:
                cli = shutil.which("code.cmd") or shutil.which("code")
                if cli:
                    result = subprocess.run(
                        [cli, "--install-extension", "mhutchie.git-graph"],
                        timeout=120,
                    )
                    if result.returncode:
                        print("WARN: optional Git Graph install failed; built-in graph is available")
                else:
                    print("WARN: Code CLI not found; built-in VS Code graph remains available")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
