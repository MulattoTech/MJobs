"""Structurally validate MJ-REQ-292 ledger claims without executing evidence.

This module is never runtime, review, integration, or umbrella acceptance
authority. It checks source text, schema, counters, and artifact references;
it does not certify the contents or execution of recorded evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.agent_context import validate_checkpoint

REQUIREMENT_ID = "MJ-REQ-292"
STEP_COUNT = 60
STATUSES = ("PASS", "PARTIAL", "FAIL", "NOT_RUN", "BLOCKED")
REQUIREMENT_SOURCE = "docs/spec/source/MJobs_Startup_Prompt.original.md"
AREA_SOURCE = "docs/spec/areas/delivery_control.md"
LEDGER_PATH = "planning/acceptance/MJ-REQ-292.json"
DEFAULT_CHECKPOINT_PATH = "planning/agent_handoffs/MJ-REQ-292.json"
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
TOP_LEVEL_OPTIONAL_KEYS = {"issue_url", "acceptance_policy", "specification"}
SCOREBOARD_KEYS = {"total", "verified_pass", "PARTIAL", "FAIL", "NOT_RUN", "BLOCKED"}
STEP_KEYS = {
    "number", "original_requirement", "expected_observable_result",
    "implementation_references", "status", "evidence_of_existing_behavior",
    "missing_behavior", "responsible_agent", "exclusive_source_ownership",
    "required_automated_test", "required_integration_validation", "verification",
    "review", "merge",
}
STEP_OPTIONAL_KEYS = {"phase", "ownership_status"}
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
    def structurally_valid(self) -> bool:
        return not self.errors


def _is_object(value: Any, keys: set[str], label: str, errors: list[str], *,
               optional: set[str] | None = None) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return False
    optional = optional or set()
    missing, extra = keys - value.keys(), value.keys() - (keys | optional)
    if missing:
        errors.append(f"{label} missing fields: {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"{label} has unknown fields: {', '.join(sorted(extra))}")
    return not missing and not extra


def _utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value)
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
    numbered_requirements: list[tuple[int, str]] = []
    for line in body.splitlines():
        match = re.fullmatch(r"(\d+)\. (.+)", line)
        if match:
            numbered_requirements.append((int(match.group(1)), match.group(2)))
    expected_numbers = list(range(1, STEP_COUNT + 1))
    if [number for number, _ in numbered_requirements] != expected_numbers:
        raise ValueError(f"{path.as_posix()} section 292 labels must be exactly 1 through {STEP_COUNT}")
    return [requirement for _, requirement in numbered_requirements], body


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
    evidence_kind = verification.get("evidence_kind")
    if not isinstance(evidence_kind, str) or evidence_kind not in EVIDENCE_KINDS:
        errors.append(f"{label} PASS has an unknown evidence_kind")
    if step.get("number") == 11 and evidence_kind != "PUBLIC_READ_ONLY":
        errors.append(f"{label} requires PUBLIC_READ_ONLY evidence; a controlled fixture is insufficient")
    if not isinstance(verification.get("continuity"), dict) or not verification.get("continuity"):
        errors.append(f"{label} PASS requires nonempty continuity metadata")
    if step.get("missing_behavior"):
        errors.append(f"{label} PASS cannot retain missing_behavior")
    _repository_file(root, verification.get("evidence_artifact"),
                     f"{label} verification.evidence_artifact", errors,
                     digest=verification.get("evidence_sha256", ""))


def validate_ledger(data: Any, root: Path) -> ValidationReport:
    """Return structural findings; never execute commands or certify acceptance."""
    errors: list[str] = []
    root = Path(root).resolve()
    if not _is_object(data, TOP_LEVEL_KEYS, "ledger", errors,
                      optional=TOP_LEVEL_OPTIONAL_KEYS):
        return ValidationReport(errors, {status: 0 for status in STATUSES}, STEP_COUNT, "UNKNOWN")
    if isinstance(data.get("schema_version"), bool) or data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if data.get("requirement_id") != REQUIREMENT_ID:
        errors.append(f"requirement_id must be {REQUIREMENT_ID}")
    if "issue_url" in data and data["issue_url"] != "https://github.com/MulattoTech/MJobs/issues/293":
        errors.append("issue_url must identify MJobs issue 293")
    if "acceptance_policy" in data and (
            not isinstance(data["acceptance_policy"], str) or not data["acceptance_policy"].strip()):
        errors.append("acceptance_policy must be a nonempty string")
    if "specification" in data:
        specification = data["specification"]
        spec_keys = {"working", "original", "original_lines"}
        if _is_object(specification, spec_keys, "specification", errors):
            if specification.get("working") != AREA_SOURCE:
                errors.append(f"specification.working must be {AREA_SOURCE}")
            if specification.get("original") != REQUIREMENT_SOURCE:
                errors.append(f"specification.original must be {REQUIREMENT_SOURCE}")
            if specification.get("original_lines") != [7659, 7725]:
                errors.append("specification.original_lines must be [7659, 7725]")
    if not isinstance(data.get("baseline_commit"), str) or not SHA1_RE.fullmatch(
            data.get("baseline_commit", "")):
        errors.append("baseline_commit must be an exact 40-character lowercase SHA")
    if not _utc_timestamp(data.get("updated_at_utc")):
        errors.append("updated_at_utc must be a UTC timestamp ending in Z")
    if data.get("umbrella_result") not in STATUSES:
        errors.append("umbrella_result must use a supported status")
    elif data.get("umbrella_result") == "PASS":
        errors.append("umbrella PASS cannot be certified by structural validation; typed runtime, review and integration receipts are required")

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
        if not _is_object(step, STEP_KEYS, label, errors, optional=STEP_OPTIONAL_KEYS):
            continue
        if "phase" in step and (
                not isinstance(step["phase"], str)
                or step["phase"] not in {"A", "B", "C", "D", "E", "F"}):
            errors.append(f"{label}.phase must be one of A through F")
        if "ownership_status" in step and (
                not isinstance(step["ownership_status"], str) or not step["ownership_status"].strip()):
            errors.append(f"{label}.ownership_status must be a nonempty string")
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
        for key in ("implementation_references", "exclusive_source_ownership"):
            if not isinstance(step.get(key), list) or any(not isinstance(item, str) for item in step[key]):
                errors.append(f"step {number}.{key} must be an array of strings")
        existing = step.get("evidence_of_existing_behavior")
        if not isinstance(existing, list):
            errors.append(f"step {number}.evidence_of_existing_behavior must be an array")
        else:
            for evidence_index, pointer in enumerate(existing, start=1):
                pointer_label = f"step {number}.evidence_of_existing_behavior[{evidence_index}]"
                if not _is_object(pointer, {"artifact", "classification"}, pointer_label, errors):
                    continue
                if pointer.get("classification") != "HISTORICAL_SCOPED_NOT_CURRENT_ACCEPTANCE":
                    errors.append(f"{pointer_label}.classification must mark historical scoped evidence")
                _repository_file(root, pointer.get("artifact"), f"{pointer_label}.artifact", errors)
        for key in ("missing_behavior", "responsible_agent", "required_automated_test",
                    "required_integration_validation"):
            if not isinstance(step.get(key), str):
                errors.append(f"step {number}.{key} must be a string")
        status = step.get("status")
        if status not in STATUSES:
            errors.append(f"step {number}.status is unsupported")
        else:
            counts[status] += 1
        verification = step.get("verification")
        if _is_object(verification, VERIFICATION_KEYS,
                      f"step {number}.verification", errors):
            for key in ("tested_commit", "evidence_artifact", "evidence_sha256", "command",
                        "observed_result", "executed_at_utc", "run_id", "evidence_kind"):
                if verification.get(key) is not None and not isinstance(verification.get(key), str):
                    errors.append(f"step {number}.verification.{key} must be text or null")
            if not isinstance(verification.get("continuity"), dict):
                errors.append(f"step {number}.verification.continuity must be an object")
        review = step.get("review")
        if _is_object(review, {"status", "evidence_artifact"},
                       f"step {number}.review", errors):
            review_status = review.get("status")
            if not isinstance(review_status, str) or review_status not in {
                    "NOT_RUN", "PASS", "PARTIAL", "FAIL"}:
                errors.append(f"step {number}.review.status is unsupported")
            if review.get("evidence_artifact") is not None and not isinstance(
                    review.get("evidence_artifact"), str):
                errors.append(f"step {number}.review.evidence_artifact must be a path or null")
            if review_status == "PASS":
                _repository_file(root, review.get("evidence_artifact"),
                                 f"step {number}.review.evidence_artifact", errors)
        merge = step.get("merge")
        if _is_object(merge, {"status", "pr_url", "integrated_commit"},
                      f"step {number}.merge", errors):
            merge_status = merge.get("status")
            if not isinstance(merge_status, str) or merge_status not in {"NOT_MERGED", "MERGED"}:
                errors.append(f"step {number}.merge.status is unsupported")
            if merge_status == "MERGED":
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

    return ValidationReport(errors, counts, STEP_COUNT, data.get("umbrella_result", "UNKNOWN"))


def load_ledger(path: Path) -> Any:
    return load_ledger_bytes(Path(path).read_bytes())


def load_ledger_bytes(raw: bytes) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8-sig"),
                      object_pairs_hook=reject_duplicate_keys)


def render_report(report: ValidationReport) -> str:
    counts = report.counts
    return (f"{REQUIREMENT_ID}: {counts['PASS']}/{report.total} recorded PASS claims; "
            f"structural validation only (runtime/review/integration not certified); "
            f"{counts['PARTIAL']} PARTIAL, {counts['FAIL']} FAIL, "
            f"{counts['NOT_RUN']} NOT_RUN, {counts['BLOCKED']} BLOCKED")


def _safe_repository_path(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError(f"{label} must be a repository-relative POSIX path")
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        raise ValueError(f"{label} must not be absolute or traverse directories")
    target = (root / candidate).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"{label} escapes the repository")
    if not target.is_file():
        raise ValueError(f"{label} does not name an existing file")
    return target


def checkpoint_scoreboard(report: ValidationReport, data: dict) -> dict[str, Any]:
    """Build the projection-owned, clearly non-authoritative handoff scoreboard.

    Any narrative note formerly nested in this object belongs outside it; every
    member here is generated from the ledger or labels what those counts mean.
    """
    return {
        "acceptance_semantics": "LEDGER_COUNTS_NOT_RUNTIME_CERTIFICATION",
        "verified_pass": report.counts["PASS"],
        "total": report.total,
        "PARTIAL": report.counts["PARTIAL"],
        "FAIL": report.counts["FAIL"],
        "NOT_RUN": report.counts["NOT_RUN"],
        "BLOCKED": report.counts["BLOCKED"],
        "umbrella_result": data["umbrella_result"],
        "baseline_commit": data["baseline_commit"],
    }


def _ledger_projection_inputs(root: Path, ledger_relative: str
                              ) -> tuple[dict, ValidationReport, str]:
    ledger_path = _safe_repository_path(root, ledger_relative, "ledger path")
    raw = ledger_path.read_bytes()
    data = load_ledger_bytes(raw)
    report = validate_ledger(data, root)
    if not report.structurally_valid:
        raise ValueError("ledger structurally invalid: " + "; ".join(report.errors))
    return data, report, hashlib.sha256(raw).hexdigest()


def _checkpoint_projection_errors(checkpoint: Any, ledger_relative: str,
                                  expected_scoreboard: dict, ledger_digest: str) -> list[str]:
    if not isinstance(checkpoint, dict):
        return ["checkpoint must be an object"]
    errors = validate_checkpoint(checkpoint)
    if checkpoint.get("task_id") != REQUIREMENT_ID:
        errors.append(f"checkpoint task_id must be {REQUIREMENT_ID}")
    if checkpoint.get("acceptance_ledger") != ledger_relative:
        errors.append("checkpoint acceptance_ledger path differs from the selected ledger")
    if checkpoint.get("scoreboard") != expected_scoreboard:
        errors.append("checkpoint scoreboard differs from the current ledger projection")
    if checkpoint.get("acceptance_ledger_sha256") != ledger_digest:
        errors.append("checkpoint acceptance_ledger_sha256 differs from current ledger bytes")
    return errors


def check_checkpoint_scoreboard(root: Path, ledger_relative: str = LEDGER_PATH,
                                checkpoint_relative: str = DEFAULT_CHECKPOINT_PATH) -> list[str]:
    """Return drift findings without changing the handoff or acceptance ledger."""
    root = Path(root).resolve()
    try:
        data, report, ledger_digest = _ledger_projection_inputs(root, ledger_relative)
        checkpoint_path = _safe_repository_path(root, checkpoint_relative, "checkpoint path")
        checkpoint = load_ledger(checkpoint_path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return [str(exc)]
    return _checkpoint_projection_errors(
        checkpoint, ledger_relative, checkpoint_scoreboard(report, data), ledger_digest
    )


def write_checkpoint_scoreboard(root: Path, ledger_relative: str = LEDGER_PATH,
                                checkpoint_relative: str = DEFAULT_CHECKPOINT_PATH) -> Path:
    """Atomically replace only the projection-owned scoreboard and ledger hash."""
    root = Path(root).resolve()
    data, report, ledger_digest = _ledger_projection_inputs(root, ledger_relative)
    checkpoint_path = _safe_repository_path(root, checkpoint_relative, "checkpoint path")
    checkpoint = load_ledger(checkpoint_path)
    if not isinstance(checkpoint, dict):
        raise TypeError("checkpoint must be an object")
    errors = validate_checkpoint(checkpoint)
    if checkpoint.get("task_id") != REQUIREMENT_ID:
        errors.append(f"checkpoint task_id must be {REQUIREMENT_ID}")
    if checkpoint.get("acceptance_ledger") != ledger_relative:
        errors.append("checkpoint acceptance_ledger path differs from the selected ledger")
    if errors:
        raise ValueError("checkpoint invalid: " + "; ".join(errors))

    scoreboard = checkpoint_scoreboard(report, data)
    if (checkpoint.get("scoreboard") == scoreboard
            and checkpoint.get("acceptance_ledger_sha256") == ledger_digest):
        return checkpoint_path
    updated = dict(checkpoint)
    updated["scoreboard"] = scoreboard
    updated["acceptance_ledger_sha256"] = ledger_digest
    errors = validate_checkpoint(updated)
    if errors:
        raise ValueError("checkpoint invalid after projection: " + "; ".join(errors))

    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=checkpoint_path.parent,
                prefix=".acceptance-ledger-", suffix=".tmp", delete=False) as handle:
            temporary_path = handle.name
            json.dump(updated, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, checkpoint_path)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return checkpoint_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=LEDGER_PATH, help="repository-relative ledger JSON")
    parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT_PATH,
                        help="repository-relative MJ-REQ-292 checkpoint JSON")
    parser.add_argument("--check", action="store_true",
                        help="exit nonzero if ledger structure is invalid")
    projection = parser.add_mutually_exclusive_group()
    projection.add_argument("--check-checkpoint", action="store_true",
                            help="check checkpoint scoreboard against the current ledger")
    projection.add_argument("--write-checkpoint-scoreboard", action="store_true",
                            help="atomically refresh only checkpoint scoreboard and ledger hash")
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    try:
        ledger_path = _safe_repository_path(root, args.ledger, "ledger path")
        report = validate_ledger(load_ledger(ledger_path), root)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"{REQUIREMENT_ID}: 0/{STEP_COUNT} recorded PASS claims; ledger structure invalid: {exc}")
        return 1
    print(render_report(report))
    if not report.structurally_valid:
        for error in report.errors:
            print(f"ERROR: {error}")
        return 1
    if args.check_checkpoint:
        errors = check_checkpoint_scoreboard(root, args.ledger, args.checkpoint)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print("Checkpoint scoreboard matches ledger bytes; acceptance remains structural only.")
    elif args.write_checkpoint_scoreboard:
        try:
            write_checkpoint_scoreboard(root, args.ledger, args.checkpoint)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 1
        print("Checkpoint scoreboard and ledger hash updated from structural ledger projection.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by module entry point
    sys.exit(main())
