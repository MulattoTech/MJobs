"""Fail-closed inventory of real paths referenced by a MJobs Codex campaign.

No Git operations or model dispatch. Only paths inside the discovered checkout
are valid; historical deleted sibling checkouts are never guessed/recreated.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

REQUIRED = (
    "AGENTS.md",
    ".agents/skills/mjobs-delivery/SKILL.md",
    "planning/CAPABILITY_MATRIX.json",
    "planning/REFERENCE_GAP_MATRIX.json",
    "planning/REQUIREMENT_INDEX.json",
    "planning/ISSUE_SEED.jsonl",
    "planning/SESSION_HANDOFF.md",
)
PROMPTS = (
    "prompts/codex/project-board-end-to-end.md",
    "prompts/codex/board-p0p1-plan.md",
    "prompts/codex/board-p0p1-goal.md",
    "prompts/codex/board-p0p1-main.md",
)
RELATIVE = re.compile(
    r"^(?:AGENTS\.md|(?:\.agents|planning|tools|prompts|docs)/"
    r"[A-Za-z0-9._/+\-]+\.(?:md|json|jsonl|py|ps1|yaml|yml|toml|ts|tsx))$"
)
ABSOLUTE = re.compile(r"[A-Za-z]:\\Tools\\Dev\\MJobs[^\s\x22\x27\x60<>]*")


def inspect_paths(root: Path, prompts: tuple[str, ...] = PROMPTS,
                  required: tuple[str, ...] = REQUIRED) -> dict[str, Any]:
    root = root.resolve()
    # A reviewed isolated clone is beneath MJobs/.mjobs-recovery/active,
    # while the explicit canonical root in prompts is its existing parent.
    canonical = root
    if ".mjobs-recovery" in root.parts:
        index = root.parts.index(".mjobs-recovery")
        candidate = Path(*root.parts[:index])
        if candidate.is_dir() and candidate.name.casefold() == "mjobs":
            canonical = candidate
    found = []
    failures = []
    def check(relative: str, origin: str):
        if not isinstance(relative, str) or not relative:
            failures.append({"path": str(relative), "origin": origin, "reason": "empty"})
            return
        requested = Path(relative)
        candidate = (root / requested).resolve()
        if not candidate.is_relative_to(root):
            failures.append({"path": relative, "origin": origin, "reason": "outside repository"})
        elif not candidate.is_file():
            failures.append({"path": relative, "origin": origin, "reason": "not present"})
        else:
            found.append({"path": relative, "origin": origin})

    for relative in required:
        check(relative, "required authority")
    for relative in prompts:
        check(relative, "prompt")
        path = root / relative
        if not path.is_file():
            continue
        contents = path.read_text(encoding="utf8")
        for token in re.findall(r"`([^`]{2,200})`", contents):
            token = token.strip().replace("\\", "/")
            if RELATIVE.fullmatch(token):
                check(token, relative)
        for absolute in ABSOLUTE.findall(contents):
            value = absolute.rstrip(".,;:)]}")
            if value.casefold() == str(canonical).casefold() and canonical.is_dir():
                found.append({"path": value, "origin": relative})
            elif not Path(value).resolve().is_relative_to(canonical):
                failures.append({
                    "path": value, "origin": relative,
                    "reason": "obsolete or outside MJobs root",
                })
            elif not Path(value).exists():
                failures.append({
                    "path": value, "origin": relative, "reason": "not present",
                })
            else:
                found.append({"path": value, "origin": relative})
    unique = list({(x["path"], x["origin"]): x for x in failures}.values())
    return {"ok": not unique, "root": str(root),
            "verified": len(found), "missing": unique, "prompts": list(prompts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--prompt", action="append")
    args = parser.parse_args()
    prompt_set = tuple(args.prompt) if args.prompt else PROMPTS
    result = inspect_paths(args.root, prompts=prompt_set)
    print(json.dumps(result, indent=2))
    if not result["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
