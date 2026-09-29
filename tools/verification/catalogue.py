"""Validate durable requirement coverage without reading the original prompt."""
from __future__ import annotations

import json
from pathlib import Path
import re


def validate_catalogue(root: Path) -> list[str]:
    seeds = [json.loads(line) for line in (root / "planning/ISSUE_SEED.jsonl")
             .read_text(encoding="utf-8").splitlines() if line.strip()]
    items = json.loads((root / "planning/PR_QUEUE.json").read_text(encoding="utf-8"))["items"]
    errors = []
    ids = set()
    for seed in seeds:
        requirement = seed["id"]
        if requirement in ids:
            errors.append(f"Duplicate requirement: {requirement}")
        ids.add(requirement)
        markers = re.findall(r"<!-- mj-requirement:(.*?) -->", seed["body"])
        if markers != [requirement]:
            errors.append(f"Invalid requirement marker: {requirement}")
    queue = {}
    covered = set()
    for item in items:
        if item["id"] in queue:
            errors.append(f"Duplicate queue entry: {item['id']}")
        queue[item["id"]] = item
        covered.update(item["requirements"])
    errors.extend(f"Unqueued requirement: {rid}" for rid in sorted(ids - covered))
    errors.extend(f"Unknown requirement: {rid}" for rid in sorted(covered - ids))
    visited, visiting = set(), set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            errors.append(f"Dependency cycle: {identifier}")
            return
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in queue[identifier].get("depends_on", []):
            if dependency not in queue:
                errors.append(f"Unknown dependency: {identifier} -> {dependency}")
            else:
                visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in queue:
        visit(identifier)
    return errors


if __name__ == "__main__":
    problems = validate_catalogue(Path(__file__).resolve().parents[2])
    for problem in problems:
        print(problem)
    print(f"Catalogue validation: {'FAIL' if problems else 'PASS'}")
    raise SystemExit(bool(problems))
