"""Offline, dependency-free validation of Feature Studio v1 intake records.

This validates the supported subset of JSON Schema used by schema.v1.json
and checks lifecycle/execution invariants. It does not execute agents.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STUDIO = ROOT / "feature-studio"


def check_against_schema(value: Any, schema: dict[str, Any], where: str = "$") -> list[str]:
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: not one of {schema['enum']}")
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    if expected and (not isinstance(value, types[expected]) or
                     (expected == "integer" and isinstance(value, bool))):
        return errors + [f"{where}: expected {expected}"]
    if expected == "object":
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}.{key}: required")
        if schema.get("additionalProperties") is False:
            for key in value.keys() - schema.get("properties", {}).keys():
                errors.append(f"{where}.{key}: unknown field")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                errors += check_against_schema(value[key], child, f"{where}.{key}")
    elif expected == "array":
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{where}: too few items")
        if "items" in schema:
            for index, item in enumerate(value):
                errors += check_against_schema(item, schema["items"], f"{where}[{index}]")
    elif expected == "string":
        if len(value.strip()) < schema.get("minLength", 0):
            errors.append(f"{where}: too short")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errors.append(f"{where}: invalid format")
    return errors


def validate_spec(record: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    errors = check_against_schema(record, schema)
    if errors:
        return errors
    criteria = record["acceptance_criteria"]
    ids = [item["id"] for item in criteria]
    if len(set(ids)) != len(ids):
        errors.append("acceptance_criteria: duplicate criterion ids")
    for item in criteria:
        if item["status"] == "PASS" and not item["evidence"]:
            errors.append(f"{item['id']}: PASS requires verifiable evidence")
    lifecycle = record["lifecycle"]
    execution = record["execution"]
    if lifecycle in {"APPROVED", "IMPLEMENTING", "VERIFIED", "CLOSED"} and not execution["spec_review_approved"]:
        errors.append("lifecycle: advancement requires recorded specification approval")
    if lifecycle in {"VERIFIED", "CLOSED"} and any(item["status"] != "PASS" for item in criteria):
        errors.append("lifecycle: VERIFIED/CLOSED requires every criterion PASS")
    if execution["agent_execution_authorized"] and not execution["spec_review_approved"]:
        errors.append("execution: agent authorization requires approved specification")
    if execution["automated_main_push"]:
        errors.append("execution: automatic main pushes are prohibited in this pilot")
    if not execution["human_merge_review_required"]:
        errors.append("execution: human review is mandatory")
    return errors


def validate_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {"version": int, "pilot_repository": str, "active_coverage": list,
                "agent_execution_enabled": bool, "git_write_integration_enabled": bool,
                "org_rollout_approved": bool, "human_merge_approval_required": bool}
    for key, expected in required.items():
        if type(config.get(key)) is not expected:
            errors.append(f"config.{key}: expected {expected.__name__}")
    if errors:
        return errors
    if config["version"] != 1:
        errors.append("config.version: unsupported version")
    if config["pilot_repository"] not in config["active_coverage"]:
        errors.append("config: pilot repository must be covered")
    if config["agent_execution_enabled"] or config["git_write_integration_enabled"] or config["org_rollout_approved"]:
        errors.append("config: pilot cannot enable remote execution, GitHub writes, or organization rollout")
    if not config["human_merge_approval_required"]:
        errors.append("config: human approval cannot be disabled")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Feature Studio specs (no GitHub writes)")
    parser.add_argument("--all", action="store_true", help="Validate all repository feature specs and pilot config")
    parser.add_argument("--spec", type=Path, help="Validate a single JSON spec")
    args = parser.parse_args()
    schema = json.loads((STUDIO / "schema.v1.json").read_text(encoding="utf-8"))
    paths = sorted((STUDIO / "specs").glob("*.json")) if args.all or not args.spec else [args.spec]
    errors: list[str] = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            found = validate_spec(record, schema)
        except (OSError, ValueError, TypeError) as exc:
            found = [f"unable to parse specification: {exc}"]
        if found:
            errors.extend(f"{path}: {issue}" for issue in found)
        else:
            print(f"SCHEMA VALID (not feature acceptance): {path}")
    if args.all or not args.spec:
        try:
            found = validate_config(json.loads((STUDIO / "config.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            found = [f"unable to parse config: {exc}"]
        errors.extend(f"config: {issue}" for issue in found)
        if not found:
            print("PILOT CONFIG VALID (no deployment performed)")
    for error in errors:
        print("INVALID:", error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
