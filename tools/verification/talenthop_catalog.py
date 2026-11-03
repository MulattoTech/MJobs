"""Validate the additive TalentHop inventory without editing acceptance authorities."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
import sys

DISCOVERY = {"OBSERVED", "PUBLICLY CLAIMED", "INACCESSIBLE / AUTHORIZATION REQUIRED", "NOT FOUND / UNKNOWN"}
IMPLEMENTATION = {"ALREADY EXISTS", "PARTIAL", "MISSING", "BLOCKED", "VERIFIED IN TEST", "MERGED", "DEPLOYED", "ACCEPTED"}
SHA = re.compile(r"[0-9a-f]{40}\Z")


def _ui_evidence_ids(evidence: list[dict], root: Path, errors: list[str]) -> set[str]:
    """Resolve observed UI claims to bounded, committed observation records."""
    verified = set()
    cache = {}
    root = root.resolve()
    for item in evidence:
        if item.get("kind") != "INTERACTIVE_UI":
            continue
        value = item.get("artifact")
        path = None
        if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./-]+", value)
                and PurePosixPath(value).parts[:2] == ("docs", "evidence")
                and ".." not in PurePosixPath(value).parts):
            candidate = (root / value).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                path = candidate
        if path is None:
            errors.append("UI_EVIDENCE_ARTIFACT_INVALID")
            continue
        if path not in cache:
            try:
                if path.stat().st_size > 1_000_000:
                    raise ValueError("Evidence exceeds the bounded document size")
                payload = json.loads(path.read_text(encoding="utf-8"))
                records = payload.get("observations")
                if (payload.get("schema_version") != 1 or not isinstance(records, list)
                        or not 1 <= len(records) <= 500
                        or any(not isinstance(record, dict) for record in records)):
                    raise ValueError("Invalid observation document")
                cache[path] = records
            except (OSError, UnicodeError, ValueError, AttributeError):
                cache[path] = None
        records = cache[path]
        matches = [record for record in records or [] if record.get("id") == item["id"]]
        if len(matches) != 1:
            errors.append("UI_EVIDENCE_RECORD_INVALID")
            continue
        record = matches[0]
        try:
            timestamp = datetime.fromisoformat(record.get("at", "").replace("Z", "+00:00"))
            valid_time = timestamp.tzinfo is not None and record["at"] == item.get("retrieved_at")
        except (ValueError, TypeError, AttributeError):
            valid_time = False
        valid_text = all(isinstance(record.get(key), str) and 0 < len(record[key]) <= 1000
                         for key in ("route", "role", "scope"))
        valid_steps = all(isinstance(record.get(key), list) and 1 <= len(record[key]) <= 50
                          and all(isinstance(text, str) and 0 < len(text) <= 2000
                                  for text in record[key])
                          for key in ("steps", "observed"))
        route = record.get("route", "")
        if (not valid_time or not valid_text or not valid_steps
                or not route.startswith("/") or route.startswith("//")
                or "?" in route or "#" in route):
            errors.append("UI_EVIDENCE_RECORD_INVALID")
            continue
        verified.add(item["id"])
    return verified


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
    if (not isinstance(evidence, list) or len(evidence) > 500
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                   or not item["id"] for item in evidence)
            or len({item["id"] for item in evidence}) != len(evidence)):
        return errors + ["EVIDENCE_IDS_INVALID"]
    evidence_ids = {item["id"] for item in evidence}
    ui_evidence_ids = _ui_evidence_ids(evidence, root, errors)
    graph = {}
    for row in rows:
        if row.get("discovery_state") not in DISCOVERY or row.get("implementation_state") not in IMPLEMENTATION:
            errors.append("FEATURE_STATE_INVALID")
        if not isinstance(row.get("issues"), list) or not row["issues"] or any(type(n) is not int or n <= 0 for n in row["issues"]):
            errors.append("FEATURE_ISSUES_INVALID")
        references = row.get("evidence")
        if (not isinstance(references, list) or not references
                or any(not isinstance(value, str) for value in references)
                or not set(references) <= evidence_ids):
            errors.append("FEATURE_EVIDENCE_INVALID")
            references = []
        observed = row.get("discovery_state") == "OBSERVED"
        if observed and not set(references) & ui_evidence_ids:
            errors.append("FEATURE_OBSERVATION_EVIDENCE_MISSING")
        journey = row.get("journey", {})
        if (not isinstance(journey, dict) or type(journey.get("exercised")) is not bool
                or not isinstance(journey.get("scope"), str) or not journey["scope"].strip()):
            errors.append("JOURNEY_SCOPE_INVALID")
        elif journey["exercised"] and (not observed or not set(references) & ui_evidence_ids):
            errors.append("JOURNEY_EVIDENCE_MISSING")
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
    exercised_journeys = sum(isinstance(row.get("journey"), dict)
                              and row["journey"].get("exercised") is True for row in rows)
    if coverage.get("journeys_exercised") != exercised_journeys:
        errors.append("JOURNEY_EXERCISED_COUNT_MISMATCH")
    routes = data.get("routes", [])
    if (not isinstance(routes, list) or len(routes) > 500
            or any(not isinstance(route, dict) for route in routes)):
        errors.append("ROUTES_INVALID")
        routes = []
    patterns = [route.get("pattern") for route in routes]
    if (any(not isinstance(pattern, str) or not pattern.startswith("/") for pattern in patterns)
            or len(set(pattern for pattern in patterns if isinstance(pattern, str))) != len(patterns)):
        errors.append("ROUTE_PATTERNS_INVALID")
    for route in routes:
        if route.get("state") not in DISCOVERY or type(route.get("exercised")) is not bool:
            errors.append("ROUTE_STATE_INVALID")
        if route.get("exercised") is True and (route.get("state") != "OBSERVED"
                or not isinstance(route.get("evidence"), str)
                or route["evidence"] not in ui_evidence_ids):
            errors.append("ROUTE_OBSERVATION_EVIDENCE_MISSING")
    if coverage.get("routes_identified") != len(routes):
        errors.append("ROUTE_COUNT_MISMATCH")
    if coverage.get("routes_exercised") != sum(route.get("exercised") is True for route in routes):
        errors.append("ROUTE_EXERCISED_COUNT_MISMATCH")
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
