"""Validate the durable MJ-REQ-292 acceptance ledger without executing evidence.

The ledger records claims; it is not an execution harness or an independent
acceptance authority. Every original step is checked against the repository's
source specification, counters are recomputed, and PASS rows must point to
present, hash-matching evidence.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


REQUIREMENT_ID = "MJ-REQ-292"
STEP_COUNT = 60
STATUSES = ("PASS", "PARTIAL", "FAIL", "NOT_RUN", "BLOCKED")
REQUIREMENT_SOURCE = "docs/spec/source/MJobs_Startup_Prompt.original.md"
AREA_SOURCE = "docs/spec/areas/delivery_control.md"
LEDGER_PATH = "planning/acceptance/MJ-REQ-292.json"
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PR_URL_RE = re.compile(r"^https://github\.com/MulattoTech/MJobs/pull/[1-9][0-9]*$")
EVIDENCE_KINDS = {
    "PUBLIC_READ_ONLY", "CONTROLLED_FIXTURE", "SYNTHETIC",
    "LOCAL_INTEGRATION", "HOSTED_CI", "MANUAL", "OTHER",
}

TOP_LEVEL_KEYS = {
    "schema_version", "requirement_id", "baseline_commit", "updated_at_utc",
    "umbrella_result", "scoreboard", "steps",
}
SCOREBOARD_KEYS = {"total", "verified_pass", "PARTIAL", "FAIL", "NOT_RUN", "BLOCKED"}
STEP_KEYS = {
    "number", "original_requirement", "expected_observable_result",
    "implementation_references", "status", "evidence_of_existing_behavior",
    "missing_behavior", "responsible_agent", "exclusive_source_ownership",
    "required_automated_test", "required_integration_validation", "verification",
    "review", "merge",
}
VERIFICATION_KEYS = {
    "tested_commit", "evidence_artifact", "evidence_sha256", "command",
    "observed_result", "executed_at_utc", "run_id", "evidence_kind", "continuity",
}


@dataclass(frozen=True)
class ValidationReport:
    errors: list[str]
    counts: dict[str, int]
    total: int
    umbrella_result: str

    @property
    def valid(self) -> bool:
        return not self.errors


def _is_object(value: Any, keys: set[str], label: str, errors: list[str]) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return False
    missing, extra = keys - value.keys(), value.keys() - keys
    if missing:
        errors.append(f"{label} missing fields: {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"{label} has unknown fields: {', '.join(sorted(extra))}")
    return not missing and not extra


def _utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _section_requirements(path: Path) -> tuple[list[str], str]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    heading = "# 292. END-TO-END ACCEPTANCE TEST"
    if text.count(heading) != 1:
        raise ValueError(f"{path.as_posix()} must contain exactly one section 292")
    section = text.split(heading, 1)[1]
    next_heading = re.search(r"^# 293\.", section, re.MULTILINE)
    if next_heading is None:
        raise ValueError(f"{path.as_posix()} section 292 has no section 293 boundary")
    body = section[:next_heading.start()].strip("\n")
    requirements: list[str] = []
    for line in body.splitlines():
        match = re.fullmatch(r"(\d+)\. (.+)", line)
        if match:
            requirements.append(match.group(2))
    return requirements, body


def _source_requirements(root: Path, errors: list[str]) -> list[str]:
    try:
        original, original_body = _section_requirements(root / REQUIREMENT_SOURCE)
        area, area_body = _section_requirements(root / AREA_SOURCE)
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(f"original requirement source unavailable or malformed: {exc}")
        return []
    if original_body != area_body:
        errors.append("delivery_control section 292 differs from the original source")
    if len(original) != STEP_COUNT or len(area) != STEP_COUNT:
        errors.append(f"section 292 must contain exactly {STEP_COUNT} numbered requirements")
    if original != area:
        errors.append("delivery_control numbered requirements differ from the original source")
    return original


def _repository_file(root: Path, relative: Any, label: str,
                     errors: list[str], *, digest: Any = None) -> Path | None:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        errors.append(f"{label} must be a repository-relative POSIX path")
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        errors.append(f"{label} must not be absolute or traverse directories")
        return None
    target = (root / candidate).resolve()
    if not target.is_relative_to(root.resolve()):
        errors.append(f"{label} escapes the repository")
        return None
    if not target.is_file():
        errors.append(f"{label} does not name an existing file")
        return None
    if digest is not None:
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            errors.append(f"{label} requires a lowercase SHA-256 digest")
        elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            errors.append(f"{label} SHA-256 does not match current artifact bytes")
    return target


def _validate_pass(step: dict, root: Path, label: str, errors: list[str]) -> None:
    verification = step.get("verification")
    if not isinstance(verification, dict):
        return
    for key in ("tested_commit", "command", "observed_result", "executed_at_utc", "run_id",
                "evidence_kind", "continuity", "evidence_artifact", "evidence_sha256"):
        if verification.get(key) in (None, "", {}):
            errors.append(f"{label} PASS requires verification.{key}")
    if not isinstance(verification.get("tested_commit"), str) or not SHA1_RE.fullmatch(
            verification.get("tested_commit", "")):
        errors.append(f"{label} PASS requires an exact 40-character lowercase tested_commit")
    command = verification.get("command")
    if not isinstance(command, str) or not command.strip() or command.strip().lower() in {
            "tbd", "todo", "not run", "none", "n/a"}:
        errors.append(f"{label} PASS requires a meaningful command or procedure description")
    if not isinstance(verification.get("observed_result"), str) or verification.get("observed_result") != "PASS":
        errors.append(f"{label} PASS requires observed_result=PASS")
    if not isinstance(verification.get("run_id"), str) or not verification["run_id"].strip():
        errors.append(f"{label} PASS requires a nonempty run_id")
    if not _utc_timestamp(verification.get("executed_at_utc")):
        errors.append(f"{label} PASS requires a UTC executed_at_utc timestamp")
    if verification.get("evidence_kind") not in EVIDENCE_KINDS:
        errors.append(f"{label} PASS has an unknown evidence_kind")
    if step.get("number") == 11 and verification.get("evidence_kind") != "PUBLIC_READ_ONLY":
        errors.append(f"{label} requires PUBLIC_READ_ONLY evidence; a controlled fixture is insufficient")
    if not isinstance(verification.get("continuity"), dict) or not verification.get("continuity"):
        errors.append(f"{label} PASS requires nonempty continuity metadata")
    if step.get("missing_behavior"):
        errors.append(f"{label} PASS cannot retain missing_behavior")
    _repository_file(root, verification.get("evidence_artifact"),
                     f"{label} verification.evidence_artifact", errors,
                     digest=verification.get("evidence_sha256", ""))


def _validate_full_acceptance(step: dict, root: Path, label: str, errors: list[str]) -> None:
    review = step.get("review")
    merge = step.get("merge")
    if not isinstance(review, dict) or review.get("status") != "PASS":
        errors.append(f"{label} full acceptance requires review.status=PASS")
    elif review.get("evidence_artifact"):
        _repository_file(root, review["evidence_artifact"], f"{label} review.evidence_artifact", errors)
    else:
        errors.append(f"{label} full acceptance requires a review evidence artifact")
    if not isinstance(merge, dict) or merge.get("status") != "MERGED":
        errors.append(f"{label} full acceptance requires merge.status=MERGED")
        return
    if not isinstance(merge.get("pr_url"), str) or not PR_URL_RE.fullmatch(merge["pr_url"]):
        errors.append(f"{label} merged acceptance requires a MJobs pull request URL")
    if not isinstance(merge.get("integrated_commit"), str) or not SHA1_RE.fullmatch(
            merge["integrated_commit"]):
        errors.append(f"{label} merged acceptance requires an exact integrated commit SHA")


def validate_ledger(data: Any, root: Path) -> ValidationReport:
    """Return structural findings; never execute commands or infer new PASS rows."""
    errors: list[str] = []
    root = Path(root).resolve()
    if not _is_object(data, TOP_LEVEL_KEYS, "ledger", errors):
        return ValidationReport(errors, {status: 0 for status in STATUSES}, 0, "UNKNOWN")
    if isinstance(data.get("schema_version"), bool) or data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if data.get("requirement_id") != REQUIREMENT_ID:
        errors.append(f"requirement_id must be {REQUIREMENT_ID}")
    if not isinstance(data.get("baseline_commit"), str) or not SHA1_RE.fullmatch(
            data.get("baseline_commit", "")):
        errors.append("baseline_commit must be an exact 40-character lowercase SHA")
    if not _utc_timestamp(data.get("updated_at_utc")):
        errors.append("updated_at_utc must be a UTC timestamp ending in Z")
    if data.get("umbrella_result") not in (*STATUSES,):
        errors.append("umbrella_result must use a supported status")

    originals = _source_requirements(root, errors)
    steps = data.get("steps")
    counts = {status: 0 for status in STATUSES}
    if not isinstance(steps, list):
        errors.append("steps must be an array")
        steps = []
    if len(steps) != STEP_COUNT:
        errors.append(f"steps must contain exactly {STEP_COUNT} rows")
    seen: set[int] = set()
    for position, step in enumerate(steps, start=1):
        label = f"step[{position}]"
        if not _is_object(step, STEP_KEYS, label, errors):
            continue
        number = step.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= STEP_COUNT:
            errors.append(f"{label}.number must be an integer from 1 to {STEP_COUNT}")
            continue
        if number in seen:
            errors.append(f"step {number} is duplicated")
        seen.add(number)
        if number != position:
            errors.append(f"step {position} must be numbered {position}")
        if number <= len(originals) and step.get("original_requirement") != originals[number - 1]:
            errors.append(f"step {number} original_requirement differs from section 292")
        for key in ("original_requirement", "expected_observable_result"):
            if not isinstance(step.get(key), str) or not step[key].strip():
                errors.append(f"step {number} requires nonempty {key}")
        for key in ("implementation_references", "evidence_of_existing_behavior",
                    "exclusive_source_ownership"):
            if not isinstance(step.get(key), list) or any(not isinstance(item, str) for item in step[key]):
                errors.append(f"step {number}.{key} must be an array of strings")
        for key in ("missing_behavior", "responsible_agent", "required_automated_test",
                    "required_integration_validation"):
            if not isinstance(step.get(key), str):
                errors.append(f"step {number}.{key} must be a string")
        status = step.get("status")
        if status not in STATUSES:
            errors.append(f"step {number}.status is unsupported")
        else:
            counts[status] += 1
        _is_object(step.get("verification"), VERIFICATION_KEYS,
                   f"step {number}.verification", errors)
        review = step.get("review")
        if _is_object(review, {"status", "evidence_artifact"},
                       f"step {number}.review", errors):
            if review.get("status") not in {"NOT_RUN", "PASS", "PARTIAL", "FAIL"}:
                errors.append(f"step {number}.review.status is unsupported")
            if review.get("evidence_artifact") is not None and not isinstance(
                    review.get("evidence_artifact"), str):
                errors.append(f"step {number}.review.evidence_artifact must be a path or null")
            if review.get("status") == "PASS":
                _repository_file(root, review.get("evidence_artifact"),
                                 f"step {number}.review.evidence_artifact", errors)
        merge = step.get("merge")
        if _is_object(merge, {"status", "pr_url", "integrated_commit"},
                      f"step {number}.merge", errors):
            if merge.get("status") not in {"NOT_MERGED", "MERGED"}:
                errors.append(f"step {number}.merge.status is unsupported")
            if merge.get("status") == "MERGED":
                if not isinstance(merge.get("pr_url"), str) or not PR_URL_RE.fullmatch(merge["pr_url"]):
                    errors.append(f"step {number}.merge requires a MJobs pull request URL")
                if not isinstance(merge.get("integrated_commit"), str) or not SHA1_RE.fullmatch(
                        merge.get("integrated_commit", "")):
                    errors.append(f"step {number}.merge requires an exact integrated commit SHA")
        if status == "PASS":
            _validate_pass(step, root, f"step {number}", errors)

    if seen != set(range(1, STEP_COUNT + 1)):
        errors.append("steps must include every numbered requirement exactly once")
    if not _is_object(data.get("scoreboard"), SCOREBOARD_KEYS, "scoreboard", errors):
        scoreboard = {}
    else:
        scoreboard = data["scoreboard"]
    if scoreboard:
        expected = {"total": STEP_COUNT, "verified_pass": counts["PASS"],
                    **{status: count for status, count in counts.items() if status != "PASS"}}
        for key, value in expected.items():
            if scoreboard.get(key) != value:
                errors.append(f"scoreboard.{key} is {scoreboard.get(key)!r}; recomputed value is {value}")
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
               for value in scoreboard.values()):
            errors.append("scoreboard counters must be nonnegative integers")

    if data.get("umbrella_result") == "PASS":
        if counts["PASS"] != STEP_COUNT:
            errors.append("umbrella PASS requires all 60 steps to have verified PASS status")
        for step in steps:
            if isinstance(step, dict) and step.get("status") == "PASS":
                _validate_full_acceptance(step, root, f"step {step.get('number')}", errors)
    return ValidationReport(errors, counts, STEP_COUNT, data.get("umbrella_result", "UNKNOWN"))


def load_ledger(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def render_report(report: ValidationReport) -> str:
    counts = report.counts
    return (f"{REQUIREMENT_ID}: {counts['PASS']}/{report.total} PASS "
            f"({counts['PARTIAL']} PARTIAL, {counts['FAIL']} FAIL, "
            f"{counts['NOT_RUN']} NOT_RUN, {counts['BLOCKED']} BLOCKED)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=LEDGER_PATH, help="repository-relative ledger JSON")
    parser.add_argument("--check", action="store_true", help="exit nonzero if ledger is invalid")
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    try:
        ledger_path = (root / args.ledger).resolve()
        if not ledger_path.is_relative_to(root):
            raise ValueError("ledger path escapes repository")
        report = validate_ledger(load_ledger(ledger_path), root)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"{REQUIREMENT_ID}: 0/{STEP_COUNT} PASS (ledger invalid: {exc})")
        return 1
    print(render_report(report))
    if not report.valid:
        for error in report.errors:
            print(f"ERROR: {error}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by module entry point
    sys.exit(main())
