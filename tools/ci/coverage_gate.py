"""Deterministic coverage floor for explicitly scoped production source files.

Consumes coverage.py JSON or Vitest V8 json-summary, never live provider data.
Missing files, ambiguous paths and malformed totals fail closed. The gate
emits a bounded machine-readable receipt even when a floor is not met.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

POLICY = Path(__file__).with_name("coverage_policy.json")
_EXCLUDED_PARTS = frozenset({
    "node_modules", "generated", "vendor", "private", "dist", "build", ".venv",
})


class CoverageGateError(ValueError):
    """A stable, privacy-safe invalid-report or invalid-policy classification."""


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CoverageGateError("INVALID_JSON_INPUT") from None
    if not isinstance(value, dict):
        raise CoverageGateError("INVALID_JSON_OBJECT")
    return value


def _normalized(value: str) -> str:
    return value.replace("\\", "/").removeprefix("./").strip("/")


def _policy_for(policy: dict[str, Any], kind: str) -> tuple[list[str], float]:
    if policy.get("schema_version") != 1:
        raise CoverageGateError("INVALID_POLICY_VERSION")
    config = policy.get(kind)
    if not isinstance(config, dict):
        raise CoverageGateError("MISSING_POLICY_KIND")
    paths = config.get("files")
    floor = config.get("minimum_lines_pct")
    if (not isinstance(paths, list) or not paths
            or any(not isinstance(p, str) or not p for p in paths)
            or len(set(paths)) != len(paths)):
        raise CoverageGateError("INVALID_POLICY_FILES")
    if (not isinstance(floor, (int, float)) or isinstance(floor, bool)
            or not 0 < floor <= 100):
        raise CoverageGateError("INVALID_POLICY_FLOOR")
    for path in paths:
        parts = Path(_normalized(path)).parts
        if (path.startswith(("/", "\\")) or ".." in parts
                or ":" in _normalized(path).split("/")[0]
                or any(part.casefold() in _EXCLUDED_PARTS for part in parts)):
            raise CoverageGateError("UNSAFE_POLICY_SOURCE")
    return paths, float(floor)


def _counts(kind: str, entry: Any) -> tuple[int, int]:
    if not isinstance(entry, dict):
        raise CoverageGateError("INVALID_COVERAGE_ENTRY")
    if kind == "backend":
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            raise CoverageGateError("INVALID_COVERAGE_ENTRY")
        covered, total = summary.get("covered_lines"), summary.get("num_statements")
    else:
        lines = entry.get("lines")
        if not isinstance(lines, dict):
            raise CoverageGateError("INVALID_COVERAGE_ENTRY")
        covered, total = lines.get("covered"), lines.get("total")
    if (type(covered) is not int or type(total) is not int
            or not 0 <= covered <= total or total == 0):
        raise CoverageGateError("INVALID_COVERAGE_TOTAL")
    return covered, total


def measure(kind: str, report: dict[str, Any],
            policy: dict[str, Any]) -> dict[str, Any]:
    """Compare exact selected files; never accept an incomplete coverage report."""
    if kind not in {"backend", "frontend"}:
        raise CoverageGateError("INVALID_KIND")
    wanted, floor = _policy_for(policy, kind)
    entries = report.get("files") if kind == "backend" else {
        key: value for key, value in report.items() if key != "total"
    }
    if not isinstance(entries, dict):
        raise CoverageGateError("INVALID_COVERAGE_FILES")
    results = []
    for expected in wanted:
        normalized = _normalized(expected)
        matches = [
            key for key in entries
            if isinstance(key, str) and (
                _normalized(key) == normalized
                or _normalized(key).endswith("/" + normalized)
            )
        ]
        if len(matches) != 1:
            raise CoverageGateError("MISSING_OR_AMBIGUOUS_SOURCE")
        covered, total = _counts(kind, entries[matches[0]])
        results.append({"path": expected, "covered": covered, "total": total})
    covered = sum(item["covered"] for item in results)
    total = sum(item["total"] for item in results)
    percent = covered * 100 / total
    return {
        "schema_version": 1,
        "kind": kind,
        "status": "PASS" if percent >= floor else "FAIL",
        "covered_lines": covered,
        "total_lines": total,
        "measured_lines_pct": round(percent, 2),
        "minimum_lines_pct": floor,
        "files": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=("backend", "frontend"))
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--policy", type=Path, default=POLICY)
    args = parser.parse_args(argv)
    try:
        receipt = measure(args.kind, _json_object(args.report), _json_object(args.policy))
        code = 0 if receipt["status"] == "PASS" else 1
    except CoverageGateError as error:
        receipt = {
            "schema_version": 1, "kind": args.kind,
            "status": "ERROR", "error_code": str(error),
        }
        code = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
