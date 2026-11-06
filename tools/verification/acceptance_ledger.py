"""Validate MJ-REQ-292 ledger claims without executing evidence.

Default mode is structural only. Opt-in proof mode checks byte-bound producer,
review, and protected-integration receipts offline; it cannot authenticate
their authors or establish fresh runtime/GitHub state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
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
TOP_LEVEL_OPTIONAL_KEYS = {"issue_url", "acceptance_policy", "specification", "journey_report"}
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
VERIFICATION_OPTIONAL_KEYS = {
    "job_scope", "resume_imported", "stage_lineage", "employer_discovery_lineage",
}
REVIEW_OPTIONAL_KEYS = {"evidence_sha256"}
MERGE_OPTIONAL_KEYS = {"evidence_artifact", "evidence_sha256"}
REQUIRED_PROTECTED_CONTEXTS = {
    "CI / required",
    "Integration / PostgreSQL and Redis",
    "Disposable Compose / mock end-to-end",
    "Dependencies / introduced vulnerabilities",
    "Security / secret scan",
}
MAX_PROOF_BYTES = 2 * 1024 * 1024
JOURNEY_BUNDLE_ARTIFACTS = frozenset({
    "ui.json", "public.json", "monitor_prepare.json", "recovery.json",
    "monitor_after.json", "recovery-baseline.json", "monitoring-baseline.json",
    "compose-recovery.json", "startup.json", "startup-receipt.json",
    "public-browser.json", "public-observations.json",
    "public-source-setup.json",
})
JOURNEY_SEGMENT_ARTIFACTS = {
    "ui": "ui.json", "public": "public.json",
    "monitor_prepare": "monitor_prepare.json", "recovery": "recovery.json",
    "monitor_after": "monitor_after.json",
}


@dataclass(frozen=True)
class ValidationReport:
    errors: list[str]
    counts: dict[str, int]
    total: int
    umbrella_result: str
    runtime_evidence_validated: bool = False

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
    if not isinstance(value, str):
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
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        errors.append(f"{label} must be a repository-relative POSIX path")
        return None
    if any(part in ("", ".", "..") for part in relative.split("/")):
        errors.append(f"{label} must not be absolute or traverse directories")
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        errors.append(f"{label} must not be absolute or traverse directories")
        return None
    current = root
    for part in candidate.parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            break
        reparse = getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or reparse:
            errors.append(f"{label} must not traverse symbolic or reparse links")
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
        if (key == "continuity" and step.get("number") == 1
                and verification.get(key) == {}):
            continue
        if verification.get(key) in (None, "", {}):
            errors.append(f"{label} PASS requires verification.{key}")
    if not isinstance(verification.get("tested_commit"), str) or not SHA1_RE.fullmatch(
            verification.get("tested_commit", "")):
        errors.append(f"{label} PASS requires an exact 40-character lowercase tested_commit")
    command = verification.get("command")
    command_ok = (isinstance(command, str) and bool(command.strip())
                  and command.strip().lower() not in {"tbd", "todo", "not run", "none", "n/a"})
    if step.get("number") == 1 and isinstance(command, list):
        command_ok = (6 <= len(command) <= 80
                      and all(isinstance(value, str) and value and len(value) <= 512
                              for value in command))
    if not command_ok:
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
    continuity = verification.get("continuity")
    if (not isinstance(continuity, dict)
            or step.get("number") != 1 and not continuity):
        errors.append(f"{label} PASS requires nonempty continuity metadata")
    if step.get("missing_behavior"):
        errors.append(f"{label} PASS cannot retain missing_behavior")
    _repository_file(root, verification.get("evidence_artifact"),
                     f"{label} verification.evidence_artifact", errors,
                     digest=verification.get("evidence_sha256", ""))


def _read_proof_json(root: Path, relative: Any, expected_sha: Any, label: str,
                     errors: list[str]) -> tuple[dict[str, Any], Path, str] | None:
    if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
        errors.append(f"{label} requires a lowercase SHA-256 digest")
        return None
    try:
        path = _safe_repository_path(root, relative, label)
        raw = path.read_bytes()
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        errors.append(f"{label}: {exc}")
        return None
    if len(raw) > MAX_PROOF_BYTES:
        errors.append(f"{label} exceeds the {MAX_PROOF_BYTES}-byte proof limit")
        return None
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        errors.append(f"{label} SHA-256 does not match current artifact bytes")
        return None
    try:
        value = load_ledger_bytes(raw)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{label} is not valid unambiguous JSON: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label} must contain a JSON object")
        return None
    return value, path, actual_sha


def _load_bundle_reference(root: Path, report_path: Path, reference: Any,
                           label: str, errors: list[str], *,
                           allowed_names: set[str] | None = None
                           ) -> tuple[dict[str, Any], Path, str] | None:
    if not isinstance(reference, dict) or set(reference) != {"artifact", "sha256"}:
        errors.append(f"{label} must contain a fixed sibling artifact and SHA-256")
        return None
    name = reference.get("artifact")
    if (not _is_known_bundle_artifact(name) or name == "compose-recovery.json"
            or allowed_names is not None and name not in allowed_names):
        errors.append(f"{label} must name an allowlisted journey JSON sibling")
        return None
    parent = report_path.relative_to(root).parent
    return _read_proof_json(root, (parent / name).as_posix(), reference.get("sha256"), label, errors)


def _validate_bundled_recovery(raw: dict, receipt: dict, baseline: dict,
                               raw_sha: str, journey: Any,
                               errors: list[str]) -> None:
    annotation = receipt.get("recovery_artifact_path")
    normalized_annotation = annotation.replace("\\", "/") if isinstance(annotation, str) else ""
    if (receipt.get("scope") != "CONTROLLED_RECOVERY_SEGMENT"
            or receipt.get("recovery_artifact") != "compose-recovery.json"
            or not isinstance(annotation, str)
            or not (PurePosixPath(normalized_annotation).is_absolute()
                    or PureWindowsPath(annotation).is_absolute())
            or PurePosixPath(normalized_annotation).name != "compose-recovery.json"
            or receipt.get("recovery_artifact_sha256") != raw_sha
            or receipt.get("completed_at_utc") != raw.get("timestamp")
            or receipt.get("baseline_sha256") != journey.digest(baseline)
            or receipt.get("compose_target") != raw.get("compose_target")
            or receipt.get("application_id") != raw.get("application_id")):
        errors.append("journey recovery receipt differs from the byte-bound bundled raw artifact or baseline")
    for key in ("result", "external_submission_enabled", "local_persistence",
                "public_job_inventory_recovery", "container_health"):
        if receipt.get(key) != raw.get(key):
            errors.append(f"journey recovery receipt {key} differs from bundled raw artifact")


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and SHA1_RE.fullmatch(value) is not None


def _verify_review_receipt(step: dict, report_sha: str, source_commit: str,
                           root: Path, label: str, errors: list[str]) -> None:
    review = step.get("review")
    if not isinstance(review, dict) or review.get("status") != "PASS":
        errors.append(f"{label} requires a separate PASS local review receipt")
        return
    loaded = _read_proof_json(root, review.get("evidence_artifact"),
                              review.get("evidence_sha256"),
                              f"{label}.review.evidence_artifact", errors)
    if loaded is None:
        return
    receipt, _path, _sha = loaded
    required = {"schema_version", "requirement_id", "result", "reviewer", "tested_commit",
                "evidence_sha256", "approved_steps", "captured_at_utc", "scope_limits"}
    if set(receipt) != required:
        errors.append(f"{label} review receipt has an invalid field set")
        return
    reviewer = receipt.get("reviewer")
    if (type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or receipt.get("requirement_id") != REQUIREMENT_ID
            or receipt.get("result") != "APPROVED"
            or not isinstance(reviewer, dict)
            or set(reviewer) != {"name", "role", "authored_source"}
            or not isinstance(reviewer.get("name"), str) or not reviewer["name"].strip()
            or reviewer.get("role") != "independent_local_review"
            or reviewer.get("authored_source") is not False
            or reviewer.get("name") == step.get("responsible_agent")):
        errors.append(f"{label} review receipt lacks an independent non-author reviewer identity")
    if receipt.get("tested_commit") != source_commit:
        errors.append(f"{label} review receipt tested_commit differs from the journey source")
    if receipt.get("evidence_sha256") != report_sha:
        errors.append(f"{label} review receipt is not bound to the exact journey report bytes")
    approved = receipt.get("approved_steps")
    if (not isinstance(approved, list) or not approved
            or any(type(number) is not int or not 1 <= number <= STEP_COUNT for number in approved)
            or len(set(approved)) != len(approved)
            or step.get("number") not in approved):
        errors.append(f"{label} review receipt does not approve this exact numbered requirement")
    limits = receipt.get("scope_limits")
    if (not isinstance(limits, list) or not limits
            or any(not isinstance(value, str) or not value.strip() for value in limits)):
        errors.append(f"{label} review receipt requires explicit scope limits")
    if not _utc_timestamp(receipt.get("captured_at_utc")):
        errors.append(f"{label} review receipt requires a UTC capture timestamp")


def _verify_merge_receipt(step: dict, source_commit: str,
                          root: Path, label: str, errors: list[str]) -> None:
    merge = step.get("merge")
    if not isinstance(merge, dict) or merge.get("status") != "MERGED":
        errors.append(f"{label} requires a separate MERGED protected-integration receipt")
        return
    loaded = _read_proof_json(root, merge.get("evidence_artifact"),
                              merge.get("evidence_sha256"),
                              f"{label}.merge.evidence_artifact", errors)
    if loaded is None:
        return
    receipt, _path, _sha = loaded
    required = {"schema_version", "requirement_id", "result", "pr_url", "head_commit",
                "base_commit", "merged_commit", "tested_commit", "checks",
                "unresolved_threads", "captured_at_utc"}
    if set(receipt) != required:
        errors.append(f"{label} protected-integration receipt has an invalid field set")
        return
    if (type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or receipt.get("requirement_id") != REQUIREMENT_ID
            or receipt.get("result") != "MERGED"):
        errors.append(f"{label} protected-integration receipt is not a schema 1 MERGED receipt")
    pr_url = receipt.get("pr_url")
    if (not isinstance(pr_url, str) or not PR_URL_RE.fullmatch(pr_url)
            or merge.get("pr_url") != pr_url):
        errors.append(f"{label} protected-integration receipt PR URL differs from the ledger row")
    for key in ("head_commit", "base_commit", "merged_commit", "tested_commit"):
        if not _valid_sha(receipt.get(key)):
            errors.append(f"{label} protected-integration receipt requires exact {key}")
    if receipt.get("merged_commit") != source_commit or receipt.get("tested_commit") != source_commit:
        errors.append(f"{label} journey and protected integration must use the exact merged tested commit")
    if merge.get("integrated_commit") != receipt.get("merged_commit"):
        errors.append(f"{label} integrated_commit differs from the protected-integration receipt")
    if type(receipt.get("unresolved_threads")) is not int or receipt["unresolved_threads"] != 0:
        errors.append(f"{label} protected-integration receipt has unresolved review threads")
    checks = receipt.get("checks")
    if not isinstance(checks, list) or len(checks) != len(REQUIRED_PROTECTED_CONTEXTS):
        errors.append(f"{label} requires exactly the five protected check contexts")
    else:
        names: list[str] = []
        head = receipt.get("head_commit")
        for check in checks:
            if not isinstance(check, dict) or set(check) != {"name", "result", "head_commit", "url"}:
                errors.append(f"{label} protected check has an invalid field set")
                continue
            names.append(check.get("name"))
            url = check.get("url")
            if (check.get("result") != "SUCCESS" or check.get("head_commit") != head
                    or not isinstance(url, str)
                    or not url.startswith("https://github.com/MulattoTech/MJobs/")):
                errors.append(f"{label} protected check is failed, stale or lacks its GitHub run link")
        if set(names) != REQUIRED_PROTECTED_CONTEXTS or len(names) != len(set(names)):
            errors.append(f"{label} protected check contexts are missing, duplicate or unknown")
    if not _utc_timestamp(receipt.get("captured_at_utc")):
        errors.append(f"{label} protected-integration receipt requires a UTC capture timestamp")


def _is_known_bundle_artifact(name: Any) -> bool:
    return (isinstance(name, str) and name in JOURNEY_BUNDLE_ARTIFACTS
            and "/" not in name and "\\" not in name and ":" not in name)


def _segment_for_step(number: int) -> str:
    return ("startup" if number == 1 else "public_browser" if number in {11, 16}
            else "recovery" if 54 <= number <= 58
            else "monitor_after" if number in {59, 60} else "ui")


def _validate_recovery_inventory(report: dict, ui_baseline: dict, recovery_artifact: dict,
                                 root: Path, report_path: Path, report_sha: str,
                                 errors: list[str], *, baseline_override: dict | None = None) -> None:
    from cli import acceptance_journey as journey

    baseline = baseline_override if baseline_override is not None else ui_baseline
    derived = report.get("derived_baseline") if baseline_override is None else None
    if derived is not None:
        if not isinstance(derived, dict) or set(derived) != {"artifact", "sha256"}:
            errors.append("journey_report.derived_baseline has an invalid reference")
            return
        relative = (PurePosixPath(report_path.relative_to(root).as_posix()).parent
                    / str(derived.get("artifact", ""))).as_posix()
        loaded = _read_proof_json(root, relative, derived.get("sha256"),
                                  "journey_report.derived_baseline", errors)
        if loaded is None:
            return
        updated, _path, _sha = loaded
        segments = report.get("segments", {})
        ui_sha = segments.get("ui", {}).get("sha256")
        public_sha = segments.get("public", {}).get("sha256")
        if updated.get("baseline_lineage") != {"ui_sha256": ui_sha, "public_sha256": public_sha}:
            errors.append("journey_report derived baseline lineage differs from producer artifact hashes")
        try:
            journey._validate_recaptured_baseline(ui_baseline, updated)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"journey_report derived baseline is invalid: {exc}")
            return
        baseline = updated
    inventory = baseline.get("job_inventory") if isinstance(baseline, dict) else None
    try:
        journey.recovery.validate_public_job_inventory(inventory)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"step 55 inventory baseline is invalid: {exc}")
        return
    if inventory.get("schema_version") != journey.recovery.JOB_INVENTORY_VERSION:
        errors.append("step 55 requires the current complete versioned job inventory")
    recovered = recovery_artifact.get("public_job_inventory_recovery")
    if not isinstance(recovered, dict) or recovered.get("result") != "PASS":
        errors.append("step 55 recovery lacks positive complete inventory readback")
        return
    if any(recovered.get(key) != inventory.get(key)
           for key in ("profile_id", "sha256", "job_count", "source_copy_count")):
        errors.append("step 55 recovered inventory differs from the connected pre-restart inventory")


def _validate_runtime_evidence(data: dict, root: Path, errors: list[str]) -> bool:
    """Validate current producer bytes plus distinct offline review/merge receipts."""
    from cli import acceptance_journey as journey

    pass_steps = [step for step in data.get("steps", [])
                  if isinstance(step, dict) and step.get("status") == "PASS"]
    if not pass_steps:
        errors.append("strict proof mode requires at least one content-validated PASS row")
        return False
    if data.get("umbrella_result") == "PASS" and len(pass_steps) != STEP_COUNT:
        errors.append("umbrella PASS requires all 60 rows to be PASS")

    reference = data.get("journey_report")
    if not isinstance(reference, dict):
        errors.append("runtime evidence requires the exact journey_report artifact and SHA-256")
        return False
    loaded_report = _read_proof_json(root, reference.get("artifact"), reference.get("sha256"),
                                     "journey_report.artifact", errors)
    if loaded_report is None:
        return False
    report, report_path, report_sha = loaded_report
    if (type(report.get("schema_version")) is not int or report.get("schema_version") != 1
            or report.get("requirement_id") != REQUIREMENT_ID
            or report.get("review_status") != "NOT_RUN"):
        errors.append("journey report is not the schema 1 independent-observation report")
    run_id = report.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id):
        errors.append("journey report requires a safe run_id")
    if not _utc_timestamp(report.get("started_at_utc")) or not _utc_timestamp(report.get("updated_at_utc")):
        errors.append("journey report requires valid UTC start and completion times")
    source = report.get("source")
    try:
        if not isinstance(source, dict) or journey.validate_clean_source(source) != source:
            raise ValueError("source is not the exact clean source binding")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"journey report source is invalid: {exc}")
        return False
    target = report.get("target")
    try:
        if not isinstance(target, dict):
            raise TypeError("target is not an object")
        checked_target = journey.validate_target(
            target.get("base_url"), target.get("project_name"), target.get("port"))
        if checked_target != target:
            raise ValueError("target differs from the exact loopback fixture target")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"journey report target is invalid: {exc}")
        return False
    continuity = report.get("continuity")
    if not isinstance(continuity, dict):
        errors.append("journey report requires readback continuity identity")
        return False
    if not all(callable(getattr(journey, name, None)) for name in (
            "validate_clean_source", "validate_target", "validate_runtime_identity",
            "validate_stage_lineage", "validate_monitoring_result",
            "validate_startup_receipt", "join_public_browser", "validate_public_artifact",
            "numbered_observations", "validate_frontend_build_receipt", "source_archive_sha256",
            "_identity", "_validate_recaptured_baseline", "digest")):
        errors.append("strict proof mode requires the current startup, public-browser, runtime, lineage and monitoring validators")
        return False
    if ((any(step["number"] == 9 for step in pass_steps) or report.get("source_setup") is not None)
            and not callable(getattr(journey, "validate_source_setup_artifact", None))):
        errors.append("step 9 source setup requires the current producer join validator")
        return False
    runtime = report.get("runtime")
    if (not isinstance(runtime, dict)
            or not isinstance(runtime.get("source_manifest_sha256"), str)
            or not SHA256_RE.fullmatch(runtime.get("source_manifest_sha256", ""))
            or not isinstance(runtime.get("dockerfile_sha256"), str)
            or not SHA256_RE.fullmatch(runtime.get("dockerfile_sha256", ""))
            or not isinstance(runtime.get("compose_files"), list)
            or not runtime.get("compose_files")
            or any(not isinstance(item, str) or not item for item in runtime["compose_files"])
            or not isinstance(runtime.get("images"), dict)
            or not runtime.get("images")
            or any(not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
                   for value in runtime["images"].values())):
        errors.append("journey report requires exact runtime source, image and Compose bindings")
    if isinstance(runtime, dict):
        try:
            if runtime.get("source_manifest_sha256") != journey.source_archive_sha256(source["commit"]):
                errors.append("journey runtime source manifest differs from the exact tested commit archive")
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
            errors.append(f"journey runtime source manifest cannot be validated: {exc}")
    runtime_identity = report.get("runtime_identity")
    try:
        checked_runtime_identity = journey.validate_runtime_identity(runtime_identity)
        if (not isinstance(runtime, dict)
                or checked_runtime_identity != {key: runtime.get(key)
                                                for key in ("source_manifest_sha256", "images")}):
            errors.append("journey runtime_identity differs from the exact runtime source and image bindings")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"journey runtime_identity is invalid: {exc}")
    try:
        report_lineage = journey.validate_stage_lineage(report, continuity)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"journey report stage_lineage is invalid: {exc}")
        report_lineage = {}
    if any(_segment_for_step(step["number"]) in {"ui", "public_browser"} for step in pass_steps):
        frontend = report.get("frontend_build")
        if (not isinstance(frontend, dict) or frontend.get("status") != "PASS"
                or not isinstance(frontend.get("receipt"), dict)):
            errors.append("UI PASS requires an exact-source actual frontend build receipt")
        else:
            try:
                verified_build = journey.validate_frontend_build_receipt(
                    frontend["receipt"], source, runtime)
                if (frontend.get("receipt_sha256") != verified_build["receipt_sha256"]):
                    errors.append("frontend build receipt digest differs from the canonical receipt content")
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"journey frontend build receipt is invalid: {exc}")
    report_steps = report.get("steps")
    if not isinstance(report_steps, list) or len(report_steps) != STEP_COUNT:
        errors.append("journey report requires all 60 ordered numbered rows")
        return False
    by_number: dict[int, dict] = {}
    for position, row in enumerate(report_steps, start=1):
        if (not isinstance(row, dict) or type(row.get("number")) is not int
                or row.get("number") != position or row.get("status") not in STATUSES):
            errors.append(f"journey report step[{position}] is missing or misnumbered")
            return False
        by_number[position] = row
    report_counts = {status: sum(row["status"] == status for row in report_steps)
                     for status in STATUSES}
    expected_scoreboard = {("observed_PASS" if status == "PASS" else status): count
                           for status, count in report_counts.items()}
    if report.get("scoreboard") != expected_scoreboard:
        errors.append("journey report scoreboard differs from its numbered rows")
    expected_result = ("FAIL" if report_counts["FAIL"] else
                       "PASS" if report_counts["PASS"] == STEP_COUNT else "PARTIAL")
    if report.get("result") != expected_result:
        errors.append("journey report result differs from its numbered rows")
    if data.get("umbrella_result") == "PASS" and report.get("result") != "PASS":
        errors.append("umbrella PASS requires a complete PASS journey report")

    segments = report.get("segments")
    if not isinstance(segments, dict):
        errors.append("journey report requires its completed producer segments")
        return False
    if set(segments) - set(JOURNEY_SEGMENT_ARTIFACTS):
        errors.append("journey report contains unknown producer segments")
        return False
    ui_ref = segments.get("ui")
    if not isinstance(ui_ref, dict) or ui_ref.get("status") != "COMPLETE":
        errors.append("journey report requires the completed connected UI baseline")
        return False
    report_relative = report_path.relative_to(root).as_posix()

    loaded_segments: dict[str, tuple[dict, Path, str, dict[int, dict]]] = {}
    target_base_url = target.get("base_url") if isinstance(target, dict) else None
    relevant_segments = {_segment_for_step(step["number"]) for step in pass_steps}
    relevant_segments.add("ui")
    if "monitor_after" in relevant_segments:
        relevant_segments.add("monitor_prepare")
    segments_to_load = {segment for segment in relevant_segments
                        if segment in JOURNEY_SEGMENT_ARTIFACTS}
    if "public_browser" in relevant_segments:
        segments_to_load.add("public")
    if any(step["number"] == 9 for step in pass_steps):
        segments_to_load.add("public")
    if "monitor_after" in relevant_segments:
        segments_to_load.add("monitor_prepare")
    for segment in sorted(segments_to_load):
        segment_ref = segments.get(segment)
        if not isinstance(segment_ref, dict) or segment_ref.get("status") != "COMPLETE":
            errors.append(f"journey report lacks completed {segment} producer evidence")
            continue
        name = segment_ref.get("artifact")
        if not _is_known_bundle_artifact(name) or name != JOURNEY_SEGMENT_ARTIFACTS.get(segment):
            errors.append(f"journey report {segment} artifact must use its exact allowlisted sibling filename")
            continue
        relative = (PurePosixPath(report_relative).parent / name).as_posix()
        loaded = _read_proof_json(root, relative, segment_ref.get("sha256"),
                                  f"journey report {segment} artifact", errors)
        if loaded is None:
            continue
        artifact, artifact_path, artifact_sha = loaded
        if (artifact.get("source") != source or artifact.get("acceptance_run_id") != run_id
                or not _utc_timestamp(artifact.get("timestamp"))):
            errors.append(f"journey report {segment} artifact has stale source, run or timestamp")
            continue
        if segment == "ui":
            if (artifact.get("mode") != "ONBOARDING_FIXTURE"
                    or artifact.get("result") not in {"PASS", "PARTIAL"}
                    or artifact.get("target_origin") != target_base_url):
                errors.append("journey UI baseline does not match its reported target or mode")
            try:
                if journey._identity(artifact) != continuity:
                    errors.append("journey report continuity differs from connected UI readback")
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"journey UI baseline identity is invalid: {exc}")
        if segment == "recovery":
            target = report.get("target", {})
            compose = artifact.get("compose_target", {})
            runtime = report.get("runtime", {})
            if (artifact.get("result") != "PASS"
                    or artifact.get("external_submission_enabled") is not False
                    or artifact.get("application_id") != continuity.get("application_id")
                    or not isinstance(compose, dict)
                    or compose.get("project_name") != target.get("project_name")
                    or compose.get("port") != target.get("port")
                    or compose.get("profile") != "fixture"
                    or compose.get("project_directory") != str(journey.ROOT.resolve())
                    or compose.get("compose_files") != runtime.get("compose_files")):
                errors.append("journey recovery artifact differs from the owned fixture target")
        try:
            if segment == "monitor_prepare":
                if artifact.get("observations") != []:
                    errors.append("monitor_prepare must not claim numbered acceptance observations")
                normalized = []
            else:
                normalized = journey.numbered_observations(
                    artifact, segment=segment, run_id=run_id, source=source, identity=continuity)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"journey {segment} numbered observations are invalid: {exc}")
            continue
        normalized_by_number = {row["step"]: row for row in normalized}
        for number, observation in normalized_by_number.items():
            if segment == "public" and "public_browser" in relevant_segments:
                continue
            if segment == "ui" and number == 9 and any(
                    step["number"] == 9 for step in pass_steps):
                continue
            result_row = by_number[number]
            if result_row.get("observation") != observation:
                errors.append(f"journey report step {number} differs from its producer observation")
            if result_row.get("status") != observation["status"]:
                # The runner may withhold UI credit pending an actual frontend build.
                withheld = (segment == "ui" and observation["status"] == "PASS"
                            and result_row.get("status") == "PARTIAL"
                            and result_row.get("producer_status") == "PASS"
                            and result_row.get("credit_withheld") == "FRONTEND_BUILD_NOT_BOUND")
                if not withheld:
                    errors.append(f"journey report step {number} changes producer status")
        loaded_segments[segment] = (artifact, artifact_path, artifact_sha, normalized_by_number)

    if "startup" in relevant_segments:
        startup_loaded = _load_bundle_reference(
            root, report_path, report.get("startup"), "journey_report.startup", errors,
            allowed_names={"startup.json"})
        if startup_loaded is not None:
            startup, startup_path, startup_sha = startup_loaded
            raw_startup = _read_proof_json(
                root,
                (PurePosixPath(report_path.relative_to(root).parent.as_posix())
                 / "startup-receipt.json").as_posix(),
                startup.get("receipt_sha256"), "journey startup-receipt.json", errors)
            if raw_startup is not None:
                receipt = raw_startup[0]
                try:
                    if (startup.get("schema_version") != 1 or startup.get("result") != "PASS"
                            or startup.get("scope") != "OWNED_FIXTURE_STARTUP"
                            or startup.get("receipt_artifact") != "startup-receipt.json"
                            or startup.get("source") != source or startup.get("acceptance_run_id") != run_id
                            or startup.get("timestamp") != receipt.get("completed_at_utc")):
                        errors.append("journey startup wrapper is stale or differs from its receipt scope")
                    expected_startup = journey.validate_startup_receipt(
                        receipt, source, run_id, target, startup.get("runtime"))
                    if by_number[1].get("verification", {}).get("command") != receipt.get("command"):
                        errors.append("journey step 1 command differs from the source-bound startup receipt")
                    startup_runtime = startup.get("runtime")
                    if (not isinstance(startup_runtime, dict)
                            or any(startup_runtime.get(key) != runtime.get(key)
                                   for key in ("source_manifest_sha256", "images", "compose_files",
                                               "frontend_assets", "dockerfile_sha256"))):
                        errors.append("journey startup runtime differs from the report's exact runtime identity")
                    if startup.get("acceptance_observations") != [expected_startup]:
                        errors.append("journey startup numbered observation differs from validated receipt")
                    rows = journey.numbered_observations(
                        startup, segment="startup", run_id=run_id,
                        source=source, identity=continuity)
                    if rows != [expected_startup] or by_number[1].get("observation") != expected_startup:
                        errors.append("journey step 1 differs from exact validated startup observation")
                    loaded_segments["startup"] = (startup, startup_path, startup_sha,
                                                   {1: expected_startup})
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    errors.append(f"journey startup evidence is invalid: {exc}")

    ui_data = loaded_segments.get("ui")
    if ui_data is None:
        errors.append("runtime evidence cannot establish the connected UI baseline")
        return False
    ui_baseline = ui_data[0]
    try:
        ui_identity = journey._identity(ui_baseline)
        ui_runtime = journey.validate_runtime_identity(ui_baseline.get("runtime_identity"))
        if ui_runtime != runtime_identity:
            errors.append("connected UI runtime identity differs from journey report")
        ui_lineage = journey.validate_stage_lineage(ui_baseline, continuity)
        if ui_lineage != report_lineage:
            errors.append("connected UI stage_lineage differs from journey report")
        expected_employer_lineage = ui_identity.get("employer_discovery_lineage")
        if report.get("employer_discovery_lineage") != expected_employer_lineage:
            errors.append("journey employer discovery lineage differs from connected UI readback")
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"connected UI runtime or stage lineage is invalid: {exc}")

    recovery_data = loaded_segments.get("recovery")
    derived_reference = report.get("derived_baseline")
    derived_baseline = None
    if derived_reference is not None:
        derived_loaded = _load_bundle_reference(
            root, report_path, derived_reference, "journey_report.derived_baseline", errors,
            allowed_names={"recovery-baseline.json"})
        if derived_loaded is not None:
            derived_baseline = derived_loaded[0]
            ui_segment_ref = segments.get("ui")
            public_segment_ref = segments.get("public")
            expected_lineage = {
                "ui_sha256": ui_segment_ref.get("sha256") if isinstance(ui_segment_ref, dict) else None,
                "public_sha256": public_segment_ref.get("sha256") if isinstance(public_segment_ref, dict) else None,
            }
            if derived_baseline.get("baseline_lineage") != expected_lineage:
                errors.append("journey derived baseline does not bind the exact UI and public artifacts")
            try:
                journey._validate_recaptured_baseline(ui_baseline, derived_baseline)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"journey derived recovery baseline is invalid: {exc}")
    recovery_baseline = derived_baseline or ui_baseline
    if recovery_data is not None and "monitor_after" not in relevant_segments:
        receipt = recovery_data[0]
        raw_loaded = _read_proof_json(
            root,
            (PurePosixPath(report_path.relative_to(root).parent.as_posix())
             / "compose-recovery.json").as_posix(),
            receipt.get("recovery_artifact_sha256"),
            "journey recovery raw compose-recovery.json", errors)
        if raw_loaded is not None:
            _validate_bundled_recovery(raw_loaded[0], receipt, recovery_baseline,
                                       raw_loaded[2], journey, errors)

    monitoring_original = monitoring_baseline = None
    monitoring_original_ref = report.get("monitoring_input_baseline")
    monitoring_baseline_ref = report.get("monitoring_baseline")
    if "monitor_prepare" in relevant_segments:
        monitoring_original_loaded = _load_bundle_reference(
            root, report_path, monitoring_original_ref,
            "journey_report.monitoring_input_baseline", errors,
            allowed_names={"ui.json", "recovery-baseline.json"})
        if monitoring_original_loaded is not None:
            monitoring_original = monitoring_original_loaded[0]
        prep_data = loaded_segments.get("monitor_prepare")
        if monitoring_original is not None and prep_data is not None:
            try:
                journey.validate_monitoring_result(
                    "monitor_prepare", prep_data[0], monitoring_original, run_id)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"journey monitor_prepare evidence is invalid: {exc}")
        if "monitor_after" in relevant_segments:
            monitoring_baseline_loaded = _load_bundle_reference(
                root, report_path, monitoring_baseline_ref,
                "journey_report.monitoring_baseline", errors,
                allowed_names={"monitoring-baseline.json"})
            if monitoring_baseline_loaded is not None:
                monitoring_baseline = monitoring_baseline_loaded[0]
            if (monitoring_original is not None and monitoring_baseline is not None
                    and prep_data is not None):
                try:
                    monitoring = journey._monitoring_module()
                    expected_baseline = monitoring.monitoring_recovery_baseline(
                        monitoring_original, prep_data[0], prep_data[2])
                    if monitoring_baseline != expected_baseline:
                        errors.append("monitoring_baseline differs from preparation-bound readback")
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    errors.append(f"journey monitoring baseline lineage is invalid: {exc}")
            after_data = loaded_segments.get("monitor_after")
            if recovery_data is not None and monitoring_baseline is not None:
                recovery_receipt = recovery_data[0]
                raw_loaded = _read_proof_json(
                    root,
                    (PurePosixPath(report_path.relative_to(root).parent.as_posix())
                     / "compose-recovery.json").as_posix(),
                    recovery_receipt.get("recovery_artifact_sha256"),
                    "journey recovery raw compose-recovery.json", errors)
                if raw_loaded is not None:
                    _validate_bundled_recovery(raw_loaded[0], recovery_receipt,
                                               monitoring_baseline, raw_loaded[2], journey, errors)
            if (after_data is not None and recovery_data is not None
                    and monitoring_original is not None and prep_data is not None):
                try:
                    journey.validate_monitoring_result(
                        "monitor_after", after_data[0], monitoring_original, run_id,
                        preparation=prep_data[0], recovery_evidence=recovery_data[0])
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    errors.append(f"journey monitor_after evidence is invalid: {exc}")

    if "public" in segments_to_load:
        public_data = loaded_segments.get("public")
        if public_data is not None:
            provider = report.get("public_source_provider")
            try:
                journey.validate_public_artifact(public_data[0], ui_baseline, provider, run_id)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"journey public read-only artifact is invalid: {exc}")

    step9_claimed = any(step["number"] == 9 for step in pass_steps)
    source_setup_reference = report.get("source_setup")
    if step9_claimed or source_setup_reference is not None:
        source_setup_loaded = _load_bundle_reference(
            root, report_path, source_setup_reference,
            "step 9 source setup reference" if step9_claimed else "journey_report.source_setup",
            errors, allowed_names={"public-source-setup.json"})
        if source_setup_loaded is not None:
            setup, setup_path, setup_sha = source_setup_loaded
            public_data = loaded_segments.get("public")
            if step9_claimed and public_data is None:
                errors.append("step 9 source setup requires the completed same-run public source segment")
            elif public_data is not None:
                try:
                    if type(setup.get("schema_version")) is not int or setup["schema_version"] != 2:
                        raise ValueError("source setup requires schema version 2 ownership and release receipts")
                    setup_observation = journey.validate_source_setup_artifact(
                        setup, ui_baseline, public_data[0], target, runtime)
                    if step9_claimed:
                        report_row = by_number[9]
                        if (setup_observation.get("step") != 9
                                or setup_observation.get("status") != "PASS"
                                or report_row.get("status") != "PASS"
                                or report_row.get("observation") != setup_observation):
                            errors.append("step 9 source setup differs from its positive numbered observation")
                        loaded_segments["source_setup"] = (
                            setup, setup_path, setup_sha, {9: setup_observation})
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    errors.append(f"step 9 source setup evidence is invalid: {exc}")

    if "public_browser" in relevant_segments:
        public_data = loaded_segments.get("public")
        browser_loaded = _load_bundle_reference(
            root, report_path, report.get("public_browser"),
            "journey_report.public_browser", errors, allowed_names={"public-browser.json"})
        joined_loaded = _load_bundle_reference(
            root, report_path, report.get("public_observations"),
            "journey_report.public_observations", errors,
            allowed_names={"public-observations.json"})
        if public_data is None:
            errors.append("public browser lineage requires the checked raw public source segment")
        elif browser_loaded is not None and joined_loaded is not None:
            browser, _browser_path, browser_sha = browser_loaded
            joined, joined_path, joined_sha = joined_loaded
            try:
                if journey.join_public_browser(
                        ui_baseline, public_data[0], browser, public_data[2], browser_sha,
                        runtime) != joined:
                    errors.append("public-observations differs from the runner-validated source/browser join")
                normalized = journey.numbered_observations(
                    joined, segment="public_browser", run_id=run_id,
                    source=source, identity=continuity)
                normalized_by_number = {row["step"]: row for row in normalized}
                for number, observation in normalized_by_number.items():
                    result_row = by_number[number]
                    if (result_row.get("observation") != observation
                            or result_row.get("status") != observation["status"]):
                        errors.append(f"journey public browser step {number} differs from its joined observation")
                loaded_segments["public_browser"] = (
                    joined, joined_path, joined_sha, normalized_by_number)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"journey public browser evidence is invalid: {exc}")

    if 55 in {step["number"] for step in pass_steps}:
        recovery_data = loaded_segments.get("recovery")
        if recovery_data is not None:
            _validate_recovery_inventory(
                report, ui_baseline, recovery_data[0], root, report_path, report_sha, errors,
                baseline_override=monitoring_baseline)

    for step in pass_steps:
        number = step["number"]
        label = f"step {number}"
        segment = "source_setup" if number == 9 else _segment_for_step(number)
        segment_data = loaded_segments.get(segment)
        report_row = by_number[number]
        observation = report_row.get("observation")
        verification = step.get("verification")
        report_verification = report_row.get("verification")
        if segment_data is None or not isinstance(observation, dict) or not isinstance(verification, dict) or not isinstance(report_verification, dict):
            errors.append(f"{label} PASS lacks its report observation or verification binding")
            continue
        normalized = segment_data[3].get(number)
        if normalized is None or normalized.get("status") != "PASS":
            errors.append(f"{label} PASS is not present as a positive numbered producer observation")
            continue
        expected_artifact = segment_data[1].relative_to(root).as_posix()
        for key in ("tested_commit", "run_id", "evidence_kind", "command", "executed_at_utc", "continuity"):
            if verification.get(key) != report_verification.get(key):
                errors.append(f"{label} verification.{key} differs from the journey report")
        if verification.get("tested_commit") != source.get("commit"):
            errors.append(f"{label} tested_commit differs from exact journey source")
        if verification.get("evidence_artifact") != expected_artifact:
            errors.append(f"{label} producer artifact path differs from its journey segment")
        if (verification.get("evidence_sha256") != segment_data[2]
                or report_verification.get("evidence_sha256") != segment_data[2]):
            errors.append(f"{label} producer evidence hash differs from exact segment bytes")
        if (verification.get("observed_result") != "PASS"
                or report_verification.get("observed_result") != observation.get("observable_result")):
            errors.append(f"{label} observed result is not a positive, report-bound observation")
        if (verification.get("evidence_kind") != observation.get("evidence_kind")
                or verification.get("continuity") != observation.get("continuity")):
            errors.append(f"{label} evidence kind or continuity differs from the numbered observation")
        if "job_scope" in report_verification and verification.get("job_scope") != report_verification.get("job_scope"):
            errors.append(f"{label} job_scope differs from the runner's observed job scope")
        if "resume_imported" in report_verification and verification.get("resume_imported") != report_verification.get("resume_imported"):
            errors.append(f"{label} resume lineage differs from the runner report")
        if 36 <= number <= 53:
            expected_lineage = report_lineage.get(number)
            ui_stage_lineage = ui_data[0].get("stage_lineage")
            if (expected_lineage is None
                    or report_verification.get("stage_lineage") != expected_lineage
                    or verification.get("stage_lineage") != expected_lineage
                    or not isinstance(ui_stage_lineage, list)
                    or ui_stage_lineage.count(expected_lineage) != 1):
                errors.append(f"{label} PASS lacks exact same-run application stage lineage")
        if 23 <= number <= 31:
            employer_lineage = report.get("employer_discovery_lineage")
            if (not isinstance(employer_lineage, dict)
                    or report_verification.get("employer_discovery_lineage") != employer_lineage
                    or verification.get("employer_discovery_lineage") != employer_lineage):
                errors.append(f"{label} PASS lacks exact employer discovery operation and job lineage")
        _verify_review_receipt(step, report_sha, source["commit"], root, label, errors)
        _verify_merge_receipt(step, source["commit"], root, label, errors)
    return True


def validate_ledger(data: Any, root: Path, *,
                    require_runtime_evidence: bool = False) -> ValidationReport:
    """Validate structure; optionally join producer, local review and merge receipts.

    This only validates typed, recorded local artifacts. It never executes evidence
    commands or queries GitHub, and offline receipt contents are owner-attested.
    """
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
    if "journey_report" in data:
        reference = data["journey_report"]
        if _is_object(reference, {"artifact", "sha256"}, "journey_report", errors):
            if reference.get("sha256") is not None and not isinstance(reference["sha256"], str):
                errors.append("journey_report.sha256 must be text or null")
            _repository_file(root, reference.get("artifact"), "journey_report.artifact", errors,
                             digest=reference.get("sha256"))
    if not isinstance(data.get("baseline_commit"), str) or not SHA1_RE.fullmatch(
            data.get("baseline_commit", "")):
        errors.append("baseline_commit must be an exact 40-character lowercase SHA")
    if not _utc_timestamp(data.get("updated_at_utc")):
        errors.append("updated_at_utc must be a timezone-aware UTC timestamp")
    if data.get("umbrella_result") not in STATUSES:
        errors.append("umbrella_result must use a supported status")
    elif data.get("umbrella_result") == "PASS" and not require_runtime_evidence:
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
                      f"step {number}.verification", errors,
                      optional=VERIFICATION_OPTIONAL_KEYS):
            for key in ("tested_commit", "evidence_artifact", "evidence_sha256", "command",
                        "observed_result", "executed_at_utc", "run_id", "evidence_kind"):
                if (key == "command" and number == 1 and isinstance(verification.get(key), list)):
                    continue
                if verification.get(key) is not None and not isinstance(verification.get(key), str):
                    errors.append(f"step {number}.verification.{key} must be text or null")
            if not isinstance(verification.get("continuity"), dict):
                errors.append(f"step {number}.verification.continuity must be an object")
        review = step.get("review")
        if _is_object(review, {"status", "evidence_artifact"},
                       f"step {number}.review", errors, optional=REVIEW_OPTIONAL_KEYS):
            review_status = review.get("status")
            if not isinstance(review_status, str) or review_status not in {
                    "NOT_RUN", "PASS", "PARTIAL", "FAIL"}:
                errors.append(f"step {number}.review.status is unsupported")
            if review.get("evidence_artifact") is not None and not isinstance(
                    review.get("evidence_artifact"), str):
                errors.append(f"step {number}.review.evidence_artifact must be a path or null")
            if review.get("evidence_sha256") is not None and not isinstance(
                    review.get("evidence_sha256"), str):
                errors.append(f"step {number}.review.evidence_sha256 must be text or null")
            if review_status == "PASS":
                _repository_file(root, review.get("evidence_artifact"),
                                 f"step {number}.review.evidence_artifact", errors)
        merge = step.get("merge")
        if _is_object(merge, {"status", "pr_url", "integrated_commit"},
                      f"step {number}.merge", errors, optional=MERGE_OPTIONAL_KEYS):
            merge_status = merge.get("status")
            if not isinstance(merge_status, str) or merge_status not in {"NOT_MERGED", "MERGED"}:
                errors.append(f"step {number}.merge.status is unsupported")
            if merge.get("evidence_artifact") is not None and not isinstance(
                    merge.get("evidence_artifact"), str):
                errors.append(f"step {number}.merge.evidence_artifact must be a path or null")
            if merge.get("evidence_sha256") is not None and not isinstance(
                    merge.get("evidence_sha256"), str):
                errors.append(f"step {number}.merge.evidence_sha256 must be text or null")
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
    runtime_evidence_validated = False
    if require_runtime_evidence:
        _validate_runtime_evidence(data, root, errors)
        runtime_evidence_validated = bool(counts["PASS"]) and not errors
        if data.get("umbrella_result") == "PASS" and counts["PASS"] != STEP_COUNT:
            errors.append("umbrella PASS requires all 60 ledger rows to be PASS")
            runtime_evidence_validated = False
    return ValidationReport(errors, counts, STEP_COUNT,
                            data.get("umbrella_result", "UNKNOWN"), runtime_evidence_validated)


def load_ledger(path: Path) -> Any:
    raw = Path(path).read_bytes()
    if b"\r" in raw or raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("tracked JSON must use canonical UTF-8 LF bytes without a BOM")
    return load_ledger_bytes(raw)


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
    validation_scope = (
        "typed recorded runtime/review/integration proof content checked offline; "
        "live GitHub state not queried"
        if report.runtime_evidence_validated else
        "structural validation only (runtime/review/integration not certified)"
    )
    return (f"{REQUIREMENT_ID}: {counts['PASS']}/{report.total} recorded PASS claims; "
            f"{validation_scope}; "
            f"{counts['PARTIAL']} PARTIAL, {counts['FAIL']} FAIL, "
            f"{counts['NOT_RUN']} NOT_RUN, {counts['BLOCKED']} BLOCKED")


def _safe_repository_path(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError(f"{label} must be a repository-relative POSIX path")
    if any(part in ("", ".", "..") for part in relative.split("/")):
        raise ValueError(f"{label} must not be absolute or traverse directories")
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        raise ValueError(f"{label} must not be absolute or traverse directories")
    current = root
    for part in candidate.parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError(f"{label} does not name a safe file") from exc
        reparse = getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise ValueError(f"{label} must not traverse symbolic or reparse links")
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
    try:
        errors = validate_checkpoint(checkpoint)
    except TypeError:
        return ["checkpoint has malformed value types"]
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
    try:
        return _checkpoint_projection_errors(
            checkpoint, ledger_relative, checkpoint_scoreboard(report, data), ledger_digest
        )
    except TypeError:
        return ["checkpoint has malformed value types"]


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
                "wb", dir=checkpoint_path.parent,
                prefix=".acceptance-ledger-", suffix=".tmp", delete=False) as handle:
            temporary_path = handle.name
            handle.write((json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        current_ledger = _safe_repository_path(root, ledger_relative, "ledger path").read_bytes()
        if hashlib.sha256(current_ledger).hexdigest() != ledger_digest:
            raise ValueError("ledger changed during scoreboard projection; retry from current bytes")
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
    parser.add_argument("--require-runtime-evidence", action="store_true",
                        help="also verify referenced numbered runner, independent review and protected integration receipts offline")
    projection = parser.add_mutually_exclusive_group()
    projection.add_argument("--check-checkpoint", action="store_true",
                            help="check checkpoint scoreboard against the current ledger")
    projection.add_argument("--write-checkpoint-scoreboard", action="store_true",
                            help="atomically refresh only checkpoint scoreboard and ledger hash")
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    try:
        ledger_path = _safe_repository_path(root, args.ledger, "ledger path")
        report = validate_ledger(load_ledger(ledger_path), root,
                                  require_runtime_evidence=args.require_runtime_evidence)
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
