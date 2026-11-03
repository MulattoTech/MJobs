"""Validate the additive TalentHop inventory without editing acceptance authorities."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import sys

DISCOVERY = {"OBSERVED", "PUBLICLY CLAIMED", "INACCESSIBLE / AUTHORIZATION REQUIRED", "NOT FOUND / UNKNOWN"}
IMPLEMENTATION = {"ALREADY EXISTS", "PARTIAL", "MISSING", "BLOCKED", "VERIFIED IN TEST", "MERGED", "DEPLOYED", "ACCEPTED"}
SHA = re.compile(r"[0-9a-f]{40}\Z")


def validate_catalog(data: object, root: Path) -> list[str]:
    errors = []
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("initiative_id") != "MJ-TH":
        return ["CATALOG_SCHEMA_INVALID"]
    if data.get("repository") != "MulattoTech/MJobs" or not SHA.fullmatch(str(data.get("source_revision", ""))):
        errors.append("CATALOG_PROVENANCE_INVALID")
    rows = data.get("features")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        return errors + ["FEATURES_INVALID"]
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or any(not isinstance(value, str) or not re.fullmatch(r"MJ-TH-\d{3}", value) for value in ids) or len(set(ids)) != len(ids):
        return errors + ["FEATURE_IDS_INVALID"]
    evidence = data.get("evidence", [])
    evidence_ids = {item.get("id") for item in evidence if isinstance(item, dict)}
    graph = {}
    for row in rows:
        if row.get("discovery_state") not in DISCOVERY or row.get("implementation_state") not in IMPLEMENTATION:
            errors.append("FEATURE_STATE_INVALID")
        if not isinstance(row.get("issues"), list) or not row["issues"] or any(type(n) is not int or n <= 0 for n in row["issues"]):
            errors.append("FEATURE_ISSUES_INVALID")
        if not row.get("evidence") or not set(row["evidence"]) <= evidence_ids:
            errors.append("FEATURE_EVIDENCE_INVALID")
        dependencies = row.get("dependencies", [])
        if not isinstance(dependencies, list) or any(item not in ids for item in dependencies):
            errors.append("FEATURE_DEPENDENCIES_INVALID")
            dependencies = []
        graph[row["id"]] = dependencies
        for path in row.get("source_paths", []):
            if not isinstance(path, str) or ".." in PurePosixPath(path).parts or PurePosixPath(path).is_absolute() or "\\" in path or ":" in path:
                errors.append("FEATURE_SOURCE_PATH_INVALID")
            elif not (root / path).is_file():
                errors.append("FEATURE_SOURCE_PATH_MISSING")
        verification = row.get("verification", {})
        if row.get("implementation_state") in {"VERIFIED IN TEST", "MERGED", "DEPLOYED", "ACCEPTED"}:
            if verification.get("status") != "PASS" or not verification.get("acceptance_evidence"):
                errors.append("FEATURE_DELIVERY_EVIDENCE_MISSING")
        if row.get("implementation_state") in {"MERGED", "DEPLOYED", "ACCEPTED"} and not SHA.fullmatch(str(verification.get("merge_sha", ""))):
            errors.append("FEATURE_MERGE_EVIDENCE_MISSING")
        if row.get("implementation_state") in {"DEPLOYED", "ACCEPTED"} and not SHA.fullmatch(str(verification.get("deployed_sha", ""))):
            errors.append("FEATURE_DEPLOYMENT_EVIDENCE_MISSING")
    visited, active = set(), set()

    def visit(node):
        if node in active:
            return False
        if node in visited:
            return True
        active.add(node)
        result = all(visit(child) for child in graph.get(node, []))
        active.remove(node)
        visited.add(node)
        return result

    if not all(visit(node) for node in graph):
        errors.append("FEATURE_DEPENDENCY_CYCLE")
    audit = data.get("audit", {})
    coverage = audit.get("coverage", {})
    for group in ("routes", "journeys"):
        identified, exercised = coverage.get(group + "_identified"), coverage.get(group + "_exercised")
        if type(identified) is not int or type(exercised) is not int or not 0 <= exercised <= identified:
            errors.append("COVERAGE_INVALID")
    if coverage.get("journeys_identified") != len(rows):
        errors.append("JOURNEY_COUNT_MISMATCH")
    if coverage.get("routes_identified") != len(data.get("routes", [])):
        errors.append("ROUTE_COUNT_MISMATCH")
    har = audit.get("har", {})
    if har.get("status") in {"FULL", "PARTIAL"} and (har.get("raw_captured") is not True or not har.get("manifest")):
        errors.append("HAR_EVIDENCE_MISSING")
    if har.get("status") not in {"FULL", "PARTIAL", "BLOCKED", "NOT_RUN"}:
        errors.append("HAR_STATE_INVALID")
    if data.get("legacy_acceptance") != {"original": "planning/CAPABILITY_MATRIX.json", "reference": "planning/REFERENCE_GAP_MATRIX.json", "unchanged": True}:
        errors.append("ACCEPTANCE_AUTHORITY_CHANGED")
    return sorted(set(errors))


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    try:
        data = json.loads((root / "docs/talenthop/catalog.json").read_text(encoding="utf-8"))
        errors = validate_catalog(data, root)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        errors = ["CATALOG_INVALID"]
    print(json.dumps({"status": "FAIL" if errors else "PASS", "errors": errors}))
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
