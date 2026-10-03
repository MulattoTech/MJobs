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
            expected = {f"MJ-REF-{number:03d}" for number in range(1, 60)}
            listed = matrix.get("canonical_ids", [])
            if len(listed) != 59 or set(listed) != expected:
                errors.append("Reference catalogue must contain each MJ-REF-001..059 exactly once")
            if matrix.get("reference_gap_count") != 59 or matrix.get("original_catalogue_count") != len(ids):
                errors.append("Reference catalogue counts disagree with original and canonical catalogues")
            issues = issue_map.get("issues", {})
            if not isinstance(issues, dict):
                errors.append("Reference issue map issues must be an object")
                issues = {}
            if set(issues) != expected or any(type(value) is not int or value <= 0 for value in issues.values()):
                errors.append("Reference issue map must contain positive integer issues for all 59 canonical issues")
            statuses = matrix.get("result_by_id", {})
            if not isinstance(statuses, dict):
                errors.append("Reference completion statuses must be an object")
                statuses = {}
            if set(statuses) != expected:
                errors.append("Reference completion statuses must cover all 59 canonical issues")
            allowed = {"PASS", "FAIL", "PARTIAL", "BLOCKED_EXTERNAL", "BLOCKED_ENVIRONMENT", "NOT_RUN", "NOT_APPLICABLE"}
            for identifier, status in statuses.items():
                if not isinstance(status, dict):
                    errors.append(f"Invalid reference completion record: {identifier}")
                elif status.get("result") not in allowed or not isinstance(status.get("evidence"), list):
                    errors.append(f"Invalid reference completion record: {identifier}")
                elif status["result"] == "PASS" and not status["evidence"]:
                    errors.append(f"Reference PASS requires evidence: {identifier}")
            capability_statuses = _load_capability_statuses(root, errors)
            features = matrix.get("commercial_features")
            if not isinstance(features, dict):
                errors.append("Commercial feature records must be an object")
                features = {}
            commercial = {identifier for identifier in expected if int(identifier[-3:]) >= 43}
            area_root = root / "docs/spec/areas"
            for identifier in sorted(commercial):
                feature = features.get(identifier)
                if not isinstance(feature, dict):
                    errors.append(f"Commercial feature record missing: {identifier}")
                    continue
                _validate_commercial_feature(
                    errors, identifier, feature, ids, expected, queue, area_root,
                    statuses, capability_statuses,
                )
            _check_commercial_cycles(errors, features, reference_ids=expected)
            for identifier in sorted(set(features) - commercial):
                errors.append(f"Unexpected commercial feature record: {identifier}")
            by_issue = {}
            for identifier in sorted(expected):
                if identifier in issues and type(issues[identifier]) is int and issues[identifier] > 0:
                    by_issue.setdefault(issues[identifier], []).append(identifier)
            for issue, identifiers in by_issue.items():
                if len(identifiers) > 1:
                    for identifier in identifiers[1:]:
                        feature = features.get(identifier, {})
                        if feature.get("issue_action") != "MAP_EXISTING" or not str(feature.get("equivalence_note", "")).strip():
                            errors.append(f"Unexplained duplicate reference issue: {issue} ({identifier})")
    return errors


def _validate_commercial_feature(
    errors, identifier, feature, original_ids, reference_ids, queue, area_root,
    reference_statuses, capability_statuses,
):
    required_strings = ("title", "problem", "scope_distinction", "owner_area", "priority", "issue_action")
    for field in required_strings:
        if not isinstance(feature.get(field), str) or not feature[field].strip():
            errors.append(f"Commercial feature field required: {identifier}.{field}")
    owner_area = feature.get("owner_area")
    valid_areas = {path.stem for path in area_root.glob("*.md")}
    if isinstance(owner_area, str) and owner_area.strip() and owner_area not in valid_areas:
        errors.append(f"Invalid commercial owner area: {identifier} -> {owner_area}")
    if feature.get("priority") not in {"P3", "P4", "P5"}:
        errors.append(f"Invalid commercial priority: {identifier}")
    if feature.get("issue_action") not in {"CREATE_NEW", "MAP_EXISTING"}:
        errors.append(f"Invalid commercial issue action: {identifier}")
    if feature.get("issue_action") == "MAP_EXISTING" and not str(feature.get("equivalence_note", "")).strip():
        errors.append(f"Mapped commercial feature requires equivalence note: {identifier}")
    for field in ("original_overlaps", "reference_overlaps", "prerequisites", "queue_prerequisites", "acceptance_criteria", "test_strategy", "inspiration"):
        value = feature.get(field)
        if not isinstance(value, list):
            errors.append(f"Commercial checklist must be a list: {identifier}.{field}")
            continue
        if field in ("acceptance_criteria", "test_strategy", "inspiration") and (not value or any(not isinstance(item, str) or not item.strip() for item in value)):
            errors.append(f"Commercial checklist must contain nonempty strings: {identifier}.{field}")
        elif field in ("original_overlaps", "reference_overlaps", "prerequisites", "queue_prerequisites") and any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"Commercial references must contain strings: {identifier}.{field}")
    original_overlaps = feature.get("original_overlaps") if isinstance(feature.get("original_overlaps"), list) else []
    reference_overlaps = feature.get("reference_overlaps") if isinstance(feature.get("reference_overlaps"), list) else []
    prerequisites = feature.get("prerequisites") if isinstance(feature.get("prerequisites"), list) else []
    queue_prerequisites = feature.get("queue_prerequisites") if isinstance(feature.get("queue_prerequisites"), list) else []
    for reference in original_overlaps:
        if reference not in original_ids:
            errors.append(f"Invalid commercial original overlap: {identifier} -> {reference}")
    for reference in reference_overlaps:
        if reference not in reference_ids:
            errors.append(f"Invalid commercial reference overlap: {identifier} -> {reference}")
    for prerequisite in prerequisites:
        if not isinstance(prerequisite, str) or (prerequisite not in original_ids and prerequisite not in reference_ids):
            errors.append(f"Invalid commercial prerequisite: {identifier} -> {prerequisite}")
    for prerequisite in queue_prerequisites:
        if prerequisite not in queue:
            errors.append(f"Invalid commercial queue prerequisite: {identifier} -> {prerequisite}")
    feature_status = reference_statuses.get(identifier)
    if isinstance(feature_status, dict) and feature_status.get("result") == "PASS":
        for prerequisite in prerequisites:
            if prerequisite.startswith("MJ-REQ-"):
                status = capability_statuses.get(prerequisite)
            else:
                status = reference_statuses.get(prerequisite)
            result = status.get("result") if isinstance(status, dict) else None
            if result != "PASS":
                errors.append(
                    f"Commercial prerequisite not PASS: {identifier} -> {prerequisite}"
                )


def _load_capability_statuses(root: Path, errors: list[str]) -> dict:
    path = root / "planning/CAPABILITY_MATRIX.json"
    if not path.exists():
        return {}
    try:
        matrix = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        errors.append("Capability matrix could not be read")
        return {}
    requirements = matrix.get("requirements") if isinstance(matrix, dict) else None
    if not isinstance(requirements, list):
        errors.append("Capability matrix requirements must be a list")
        return {}
    statuses = {}
    for item in requirements:
        if not isinstance(item, dict) or not isinstance(item.get("requirement_id"), str):
            errors.append("Invalid capability matrix requirement record")
            continue
        statuses[item["requirement_id"]] = item
    return statuses



def _check_commercial_cycles(errors, features, reference_ids):
    graph = {
        identifier: [item for item in (feature.get("prerequisites") if isinstance(feature.get("prerequisites"), list) else [])
                     if isinstance(item, str) and item in reference_ids]
        for identifier, feature in features.items() if isinstance(feature, dict)
    }
    visiting, visited = set(), set()
    def visit(identifier):
        if identifier in visiting:
            errors.append(f"Commercial prerequisite cycle: {identifier}")
            return
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in graph.get(identifier, []):
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)
    for identifier in graph:
        visit(identifier)


if __name__ == "__main__":
    problems = validate_catalogue(Path(__file__).resolve().parents[2])
    for problem in problems:
        print(problem)
    print(f"Catalogue validation: {'FAIL' if problems else 'PASS'}")
    raise SystemExit(bool(problems))
