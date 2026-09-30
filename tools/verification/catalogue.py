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
    reference_matrix = root / "planning/REFERENCE_GAP_MATRIX.json"
    reference_map = root / "planning/REFERENCE_GAP_ISSUE_MAP.json"
    if reference_matrix.exists() or reference_map.exists():
        if not reference_matrix.exists() or not reference_map.exists():
            errors.append("Reference matrix and issue map must both exist")
        else:
            matrix = json.loads(reference_matrix.read_text(encoding="utf-8"))
            issue_map = json.loads(reference_map.read_text(encoding="utf-8"))
            expected = {f"MJ-REF-{number:03d}" for number in range(1, 43)}
            listed = matrix.get("canonical_ids", [])
            if len(listed) != 42 or set(listed) != expected:
                errors.append("Reference catalogue must contain each MJ-REF-001..042 exactly once")
            if matrix.get("reference_gap_count") != 42 or matrix.get("original_catalogue_count") != len(ids):
                errors.append("Reference catalogue counts disagree with original and canonical catalogues")
            issues = issue_map.get("issues", {})
            if set(issues) != expected or len(set(issues.values())) != 42:
                errors.append("Reference issue map must contain 42 distinct canonical issues")
            statuses = matrix.get("result_by_id", {})
            if set(statuses) != expected:
                errors.append("Reference completion statuses must cover all 42 canonical issues")
            allowed = {"PASS", "FAIL", "PARTIAL", "BLOCKED_EXTERNAL", "BLOCKED_ENVIRONMENT", "NOT_RUN", "NOT_APPLICABLE"}
            for identifier, status in statuses.items():
                if status.get("result") not in allowed or not isinstance(status.get("evidence"), list):
                    errors.append(f"Invalid reference completion record: {identifier}")
                elif status["result"] == "PASS" and not status["evidence"]:
                    errors.append(f"Reference PASS requires evidence: {identifier}")
    return errors


if __name__ == "__main__":
    problems = validate_catalogue(Path(__file__).resolve().parents[2])
    for problem in problems:
        print(problem)
    print(f"Catalogue validation: {'FAIL' if problems else 'PASS'}")
    raise SystemExit(bool(problems))
