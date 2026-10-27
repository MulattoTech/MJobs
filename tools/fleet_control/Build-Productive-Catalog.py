"""Inventory real missing public docstrings at an immutable Git revision. No model calls."""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import os
import subprocess
from pathlib import Path


def build(repo: Path, revision: str) -> dict:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(repo), *args], timeout=20)

    commit = git("rev-parse", "--verify", revision + "^{commit}").decode().strip()
    entries = []
    for row in git("ls-tree", "-rlz", commit).split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        parts = meta.decode().split()
        source = name.decode()
        if (
            not source.startswith(("services/", "apps/", "tools/"))
            or not source.endswith(".py")
            or source.startswith("tools/fleet_control/")
        ):
            continue
        size = int(parts[3])
        blob = parts[2]
        if not 1500 <= size <= 50000:
            continue
        try:
            text = git("show", commit + ":" + source).decode("utf-8-sig")
            tree = ast.parse(text)
        except (UnicodeError, SyntaxError):
            continue
        public = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        ]
        missing = [node for node in public if ast.get_docstring(node) is None]
        if len(public) < 2 or not missing:
            continue
        entries.append(
            {
                "path": source,
                "blob": blob,
                "missingDocstrings": len(missing),
                "publicDefinitions": len(public),
                "lines": len(text.splitlines()),
                "examples": [{"symbol": n.name, "line": n.lineno} for n in missing[:8]],
            }
        )
    entries.sort(
        key=lambda e: (-min(e["missingDocstrings"], 20), e["lines"], e["path"])
    )
    return {
        "version": 1,
        "baseCommit": commit,
        "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
        "evidenceKind": "AST public definitions without docstrings; documentation opportunity, not defect proof",
        "entries": entries[:600],
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--ref", default="origin/main")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = build(a.repo, a.ref)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    temp = a.output.with_suffix(".tmp")
    temp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(temp, a.output)
    print(
        json.dumps(
            {
                "baseCommit": result["baseCommit"],
                "opportunities": len(result["entries"]),
                "modelRequests": 0,
                "repositoryWrites": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
