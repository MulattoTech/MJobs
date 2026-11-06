"""Connected browser acceptance against the actual local Compose deployment."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit

import httpx
from docx import Document
from playwright.sync_api import sync_playwright, expect
from reportlab.pdfgen import canvas

from cli.live_feedback import capture_redacted_screenshot
from cli.compose_recovery import collect_persisted_job_inventory
from cli import local_persistence_acceptance as persistence
from cli.ui_e2e_jobs_journey import (
    build_observation, run_fixture_job_observations, validate_run_id,
)


_SAFE_OPERATION_ID = re.compile(r"[a-f0-9]{64}\Z")
_SAFE_OPERATION_ERROR = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_ERROR_TYPE_CATEGORIES = {
    "AssertionError": "BUILTIN_ASSERTION_ERROR",
    "AttributeError": "BUILTIN_ATTRIBUTE_ERROR",
    "ConnectionError": "BUILTIN_CONNECTION_ERROR",
    "FileNotFoundError": "BUILTIN_FILE_NOT_FOUND",
    "IndexError": "BUILTIN_INDEX_ERROR",
    "KeyError": "BUILTIN_KEY_ERROR",
    "OSError": "BUILTIN_OS_ERROR",
    "PermissionError": "BUILTIN_PERMISSION_ERROR",
    "RuntimeError": "BUILTIN_RUNTIME_ERROR",
    "TimeoutError": "BUILTIN_TIMEOUT_ERROR",
    "TypeError": "BUILTIN_TYPE_ERROR",
    "ValueError": "BUILTIN_VALUE_ERROR",
    "AttemptConflict": "INTERNAL_ATTEMPT_CONFLICT",
    "ControlledDryRunError": "INTERNAL_CONTROLLED_DRY_RUN",
    "FormReviewError": "INTERNAL_FORM_REVIEW",
    "OperationCancelled": "INTERNAL_OPERATION_CANCELLED",
    "OperationTimedOut": "INTERNAL_OPERATION_TIMEOUT",
    "PreparationError": "INTERNAL_PREPARATION",
    "SourceError": "INTERNAL_SOURCE",
}
_APPLICATION_STATES = {
    "PREPARED", "REVIEW_REQUIRED", "AUTH_REQUIRED", "DRY_RUN_COMPLETE",
    "SUBMITTING", "SUBMISSION_UNCONFIRMED", "CONFIRMED",
}
REVIEWED_FIXTURE_ANSWER = "Controlled fictional test application."
_PREFLIGHT_RESULTS = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "UNKNOWN"}
_ATTEMPT_STATES = {"RUNNING", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED", "AUTH_REQUIRED",
                   "USER_INTERVENTION_REQUIRED", "SUBMISSION_UNCONFIRMED", "CONFIRMED", "FAILED"}
_ATTEMPT_PHASES = {
    "NAVIGATE", "DETECT_FORM", "UPLOAD_RESUME", "UPLOAD_SUPPORTING_DOCUMENTS",
    "FILL_CONTACT", "FILL_LOCATION", "ANSWER_STANDARD", "ANSWER_CUSTOM", "VALIDATE",
    "SUBMIT", "VERIFY_CONFIRMATION",
}
_ATTEMPT_STEP_STATES = {"STARTED", "COMPLETED", "FAILED", "BLOCKED"}
_SAFE_ATTEMPT_ERROR_CODES = {
    "APPLICATION_EXECUTION_FAILED", "OPERATION_CANCELLED", "OPERATION_TIMEOUT",
    "UPLOAD_INTEGRITY_FAILED", "TIMEOUT_UNCLASSIFIED", "DATABASE_POOL_TIMEOUT",
    "PLAYWRIGHT_TIMEOUT", "REVIEW_REQUIRED", "AUTH_REQUIRED",
    "USER_INTERVENTION_REQUIRED", "SUBMISSION_UNCONFIRMED",
    "APPLICATION_REVIEW_REQUIRED", "APPLICATION_AUTH_REQUIRED",
}
_ATTEMPT_HISTORY_MAX_ENTRIES = 5
_ATTEMPT_HISTORY_MAX_STEPS = 20
_ATTEMPT_HISTORY_MAX_BYTES = 8 * 1024
_APPLICATION_READ_STATES = {"CURRENT", "UNAVAILABLE", "MISMATCH"}
_APPLICATION_PROVIDER_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "mock-ats": "Fictional mock ATS",
}


def application_provider_label(provider):
    """Return only a reviewed UI label for known canonical provider IDs."""
    return _APPLICATION_PROVIDER_LABELS.get(provider) if isinstance(provider, str) else None


def assert_mock_run_request_lineage(operation, scoped_readback, *,
                                    profile_id, application_id, request_id):
    """Bind an APPLY_MOCK operation to its request through the scoped identity.

    Ordinary durable run rows intentionally omit params.request_id. Their operation
    ID is derived from profile, application, and request ID, and the scoped request
    readback resolves that same tuple back to the admitted operation.
    """
    assert isinstance(request_id, str) and _SAFE_REQUEST_ID.fullmatch(request_id)
    from packages.persistence.store import identity

    expected_operation_id = identity(
        "application-run-request-v1", profile_id, application_id, request_id)
    assert isinstance(operation, dict)
    assert operation.get("id") == expected_operation_id
    assert operation.get("kind") == "APPLY_MOCK"
    assert operation.get("entity_id") == application_id
    params = operation.get("params")
    assert isinstance(params, dict)
    if "request_id" in params:
        assert params["request_id"] == request_id
    assert isinstance(scoped_readback, dict)
    assert scoped_readback.get("operation_id") == expected_operation_id


def submit_control_is_absent_or_disabled(locator):
    """Require no submit affordance, or exactly one disabled affordance.

    Callers must first scope this check to the selected application's visible
    mock-run panel and bind that panel to the current saved request.
    """
    count = locator.count()
    return count == 0 or (count == 1 and locator.is_disabled())


def exact_primary_question_row(question_list, expected_prompt):
    """Find exactly one row whose direct primary prompt matches the API label."""
    matches = []
    for row in question_list.locator(":scope > li").all():
        primary_prompt = row.locator(":scope > strong")
        if (primary_prompt.count() == 1
                and primary_prompt.inner_text().strip() == expected_prompt):
            matches.append(row)
    assert len(matches) == 1
    return matches[0]


def current_missing_question_by_id(question_preflight, question_id):
    """Read one unique unresolved question from the current application preflight."""
    assert isinstance(question_preflight, dict)
    assert question_preflight.get("is_current") is True
    missing = question_preflight.get("missing")
    assert isinstance(missing, list)
    matches = [row for row in missing
               if isinstance(row, dict) and row.get("question_id") == question_id]
    assert len(matches) == 1
    return matches[0]


def _attempt_history_summary(history):
    """Project the public history DTO onto a bounded set of fixed safe fields."""
    if not isinstance(history, dict) or not isinstance(history.get("attempts"), list):
        return {"read_state": "UNAVAILABLE"}
    rows = history["attempts"]
    summary = {
        "read_state": "AVAILABLE",
        "attempt_count": min(len(rows), 100),
        "has_more": history.get("has_more") is True or len(rows) > 100,
        "truncated": len(rows) > _ATTEMPT_HISTORY_MAX_ENTRIES,
        "attempts": [],
    }
    for row in rows[:_ATTEMPT_HISTORY_MAX_ENTRIES]:
        if not isinstance(row, dict):
            continue
        state = row.get("state")
        attempt = {"state": state if isinstance(state, str) and state in _ATTEMPT_STATES else "UNKNOWN"}
        phase = row.get("current_step")
        if isinstance(phase, str) and phase in _ATTEMPT_PHASES:
            attempt["current_step"] = phase
        error_code = row.get("error_code")
        if error_code is not None:
            if isinstance(error_code, str) and error_code in _SAFE_ATTEMPT_ERROR_CODES:
                attempt["error_code"] = error_code
            else:
                attempt["error_redacted"] = True
        safe_steps = []
        steps = row.get("steps")
        if isinstance(steps, list):
            for step in steps[-_ATTEMPT_HISTORY_MAX_STEPS:]:
                if not isinstance(step, str) or step.count(":") != 1:
                    continue
                phase_name, step_state = step.split(":", 1)
                if phase_name in _ATTEMPT_PHASES and step_state in _ATTEMPT_STEP_STATES:
                    safe_steps.append(f"{phase_name}:{step_state}")
        if safe_steps:
            attempt["steps"] = safe_steps
        summary["attempts"].append(attempt)
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _ATTEMPT_HISTORY_MAX_BYTES:
        return {"read_state": "UNAVAILABLE"}
    return summary


def _terminal_application_context(application, *, profile_id, application_id):
    """Project a post-terminal application read only when its ownership matches."""
    if (not isinstance(application, dict)
            or application.get("id") != application_id
            or application.get("profile_id") != profile_id):
        return {"read_state": "MISMATCH"}
    return {
        "read_state": "CURRENT",
        "application_state": application.get("state"),
        "profile_version": application.get("profile_version"),
        "document_version": application.get("document_version"),
    }


def _terminal_operation_summary(*, operation_id, state, error_code, context,
                                terminal_context=None, attempt_history=None):
    safe_state = state if state in {
        "COMPLETED", "FAILED", "CANCELLED", "BLOCKED", "DEAD_LETTERED", "UNKNOWN",
    } else "UNKNOWN"
    summary = {"kind": "APPLY_MOCK", "state": safe_state}
    error_present = error_code is not None
    summary["error_present"] = error_present
    summary["error_redacted"] = False
    if isinstance(operation_id, str) and _SAFE_OPERATION_ID.fullmatch(operation_id):
        summary["operation_id"] = operation_id
    if isinstance(error_code, str) and _SAFE_OPERATION_ERROR.fullmatch(error_code):
        summary["error_code"] = error_code
    elif error_present:
        summary["error_redacted"] = True
        category = _SAFE_ERROR_TYPE_CATEGORIES.get(error_code) if isinstance(error_code, str) else None
        if category is not None:
            summary["error_category"] = category
    if not isinstance(context, dict):
        context = {}
    application_id = context.get("application_id")
    if isinstance(application_id, str) and _SAFE_OPERATION_ID.fullmatch(application_id):
        summary["application_id"] = application_id
    request_id = context.get("request_id")
    if isinstance(request_id, str) and _SAFE_REQUEST_ID.fullmatch(request_id):
        summary["request_id"] = request_id
    profile_version = context.get("expected_profile_version")
    if isinstance(profile_version, str) and _SAFE_OPERATION_ID.fullmatch(profile_version):
        summary["expected_profile_version"] = profile_version
    document_version = context.get("expected_document_version")
    if isinstance(document_version, int) and not isinstance(document_version, bool) and document_version >= 0:
        summary["expected_document_version"] = document_version
    preflight_result = context.get("preflight_result")
    if isinstance(preflight_result, str) and preflight_result in _PREFLIGHT_RESULTS:
        summary["preflight_result"] = preflight_result
    for key in ("preflight_is_current", "submit"):
        value = context.get(key)
        if isinstance(value, bool):
            summary[key] = value
    read_state = terminal_context.get("read_state") if isinstance(terminal_context, dict) else None
    if not isinstance(read_state, str) or read_state not in _APPLICATION_READ_STATES:
        read_state = "UNAVAILABLE"
    summary["application_read_state"] = read_state
    if read_state == "CURRENT":
        application_state = terminal_context.get("application_state")
        if isinstance(application_state, str) and application_state in _APPLICATION_STATES:
            summary["application_state"] = application_state
        current_profile_version = terminal_context.get("profile_version")
        if isinstance(current_profile_version, str) and _SAFE_OPERATION_ID.fullmatch(current_profile_version):
            summary["current_profile_version"] = current_profile_version
        current_document_version = terminal_context.get("document_version")
        if isinstance(current_document_version, int) and not isinstance(current_document_version, bool) and current_document_version >= 0:
            summary["current_document_version"] = current_document_version
    if attempt_history is not None:
        summary["attempt_history"] = _attempt_history_summary(attempt_history)
    return summary


class MockRunTerminalError(RuntimeError):
    """A terminal operation failure with only bounded public identifiers."""

    def __init__(self, *, operation_id, state, error_code, context=None,
                 terminal_context=None, attempt_history=None):
        safe_state = state if state in {"FAILED", "CANCELLED", "BLOCKED", "DEAD_LETTERED"} else "UNKNOWN"
        super().__init__(f"mock run did not complete: {safe_state}")
        self.terminal_summary = _terminal_operation_summary(
            operation_id=operation_id, state=safe_state,
            error_code=error_code, context=context, terminal_context=terminal_context,
            attempt_history=attempt_history,
        )


class EvidenceJournal:
    """Atomically checkpoint only bounded, sanitized named UI evidence."""

    MAX_CHECKS = 80
    MAX_BYTES = 512 * 1024

    def __init__(self, path, binding, acceptance_run_id=None):
        self.path = Path(path)
        self.binding = binding
        if acceptance_run_id is not None:
            validate_run_id(acceptance_run_id)
        self.acceptance_run_id = acceptance_run_id
        self.acceptance_observations = []
        self.observed_job_ids = []
        self.selected_profile_id = None
        self.selected_application_id = None
        self.selected_application_job_id = None
        self.application_document_ids = []
        self.employer_discovery_lineage = None
        self.stage_lineage = []
        self.cover_letter_proof = None
        self.checks = []
        self.attempted = None
        self.failure = None
        self.overall_state = "IN_PROGRESS"
        self.remaining_state = "NOT_RUN"

    @staticmethod
    def _lineage_id(value):
        return isinstance(value, str) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is not None

    def record_observed_jobs(self, job_ids):
        for job_id in job_ids:
            if not self._lineage_id(job_id):
                raise ValueError("job readback contains an invalid identity")
            if job_id not in self.observed_job_ids:
                self.observed_job_ids.append(job_id)
        if len(self.observed_job_ids) > 100:
            raise ValueError("observed job readback exceeds limit")

    def record_employer_discovery_lineage(self, row):
        expected = {"profile_id", "operation_id", "request_id", "job_ids"}
        if (not isinstance(row, dict) or set(row) != expected
                or not self._lineage_id(row["profile_id"])
                or not isinstance(row["operation_id"], str)
                or re.fullmatch(r"[a-f0-9]{64}", row["operation_id"]) is None
                or not self._lineage_id(row["request_id"])
                or not isinstance(row["job_ids"], list)
                or not 1 <= len(row["job_ids"]) <= 100
                or any(not self._lineage_id(job_id) for job_id in row["job_ids"])
                or len(set(row["job_ids"])) != len(row["job_ids"])):
            raise ValueError("employer discovery lineage is invalid")
        if not set(row["job_ids"]) <= set(self.observed_job_ids):
            raise ValueError("employer discovery jobs lack same-profile readback")
        if (self.employer_discovery_lineage is not None
                and self.employer_discovery_lineage != row):
            raise ValueError("employer discovery lineage changed during journey")
        self.employer_discovery_lineage = {
            "profile_id": row["profile_id"], "operation_id": row["operation_id"],
            "request_id": row["request_id"], "job_ids": list(row["job_ids"]),
        }
        self.persist()

    def record_application_readback(self, *, profile_id, application_id, job_id, documents):
        if (not self._lineage_id(profile_id) or not self._lineage_id(application_id)
                or not self._lineage_id(job_id)):
            raise ValueError("application readback contains an invalid identity")
        if self.selected_profile_id not in {None, profile_id}:
            raise ValueError("selected application profile identity changed during journey")
        if self.selected_application_id not in {None, application_id}:
            raise ValueError("selected application identity changed during journey")
        if self.selected_application_job_id not in {None, job_id}:
            raise ValueError("selected application job identity changed during journey")
        self.selected_profile_id = profile_id
        self.selected_application_id = application_id
        self.selected_application_job_id = job_id
        if not isinstance(documents, list):
            raise ValueError("document readback must be a list")
        for document in documents:
            document_id = document.get("id") if isinstance(document, dict) else None
            if not self._lineage_id(document_id):
                raise ValueError("document readback contains an invalid identity")
            if document_id not in self.application_document_ids:
                self.application_document_ids.append(document_id)
        if len(self.application_document_ids) > 100:
            raise ValueError("application document readback exceeds limit")

    def record_stage_lineage(self, row):
        expected = {"step", "operation_id", "request_id", "operation_kind",
                    "application_id", "profile_id", "job_id"}
        if (not isinstance(row, dict) or set(row) != expected
                or not isinstance(row["step"], int) or isinstance(row["step"], bool)
                or row["step"] not in range(36, 54)
                or not isinstance(row["operation_id"], str)
                or re.fullmatch(r"[a-f0-9]{64}", row["operation_id"]) is None
                or any(not self._lineage_id(row[key]) for key in (
                    "request_id", "application_id", "profile_id", "job_id"))
                or not isinstance(row["operation_kind"], str)
                or row["operation_kind"] not in {"PREPARE", "APPLY_MOCK"}):
            raise ValueError("application stage lineage is invalid")
        if any(existing["step"] == row["step"] for existing in self.stage_lineage):
            raise ValueError("application stage lineage step already recorded")
        if (self.selected_profile_id != row["profile_id"]
                or self.selected_application_id != row["application_id"]
                or self.selected_application_job_id != row["job_id"]
                or self.binding.get("profile_id", row["profile_id"]) != row["profile_id"]):
            raise ValueError("application stage lineage differs from selected B identity")
        if len(self.stage_lineage) >= 18:
            raise ValueError("application stage lineage exceeds limit")
        safe_row = {key: row[key] for key in (
            "step", "operation_id", "request_id", "operation_kind",
            "application_id", "profile_id", "job_id")}
        self.stage_lineage.append(safe_row)
        self.persist()

    def record_cover_letter_proof(self, proof):
        if (not isinstance(proof, dict) or set(proof) != {"A", "B", "base_upload"}
                or set(proof["A"]) != {"application_id", "profile_id", "job_id",
                    "profile_version", "document_id", "version", "sha256"}
                or set(proof["B"]) != set(proof["A"])
                or set(proof["base_upload"]) != {"revision_id", "sha256", "unchanged"}):
            raise ValueError("cover-letter proof shape is invalid")
        records = {}
        for label in ("A", "B"):
            row = proof[label]
            if (any(not self._lineage_id(row[key]) for key in (
                    "application_id", "profile_id", "job_id", "document_id"))
                    or row["profile_id"] != self.selected_profile_id
                    or not isinstance(row["profile_version"], str)
                    or not self._lineage_id(row["profile_version"])
                    or not isinstance(row["version"], int) or isinstance(row["version"], bool)
                    or row["version"] < 1
                    or not isinstance(row["sha256"], dict)
                    or set(row["sha256"]) != {"txt", "pdf", "docx"}
                    or any(not isinstance(value, str)
                           or re.fullmatch(r"[a-f0-9]{64}", value) is None
                           for value in row["sha256"].values())):
                raise ValueError("cover-letter proof row is invalid")
            records[label] = {key: row[key] for key in (
                "application_id", "profile_id", "job_id", "profile_version",
                "document_id", "version")}
            records[label]["sha256"] = dict(row["sha256"])
        if (records["A"]["application_id"] == records["B"]["application_id"]
                or records["A"]["job_id"] == records["B"]["job_id"]
                or records["A"]["document_id"] == records["B"]["document_id"]
                or records["B"]["application_id"] != self.selected_application_id
                or records["B"]["job_id"] != self.selected_application_job_id
                or records["B"]["document_id"] not in self.application_document_ids):
            raise ValueError("cover-letter proof differs from selected B identity")
        base = proof["base_upload"]
        if (not self._lineage_id(base["revision_id"])
                or not isinstance(base["sha256"], str)
                or re.fullmatch(r"[a-f0-9]{64}", base["sha256"]) is None
                or base["unchanged"] is not True):
            raise ValueError("cover-letter base upload proof is invalid")
        self.cover_letter_proof = {**records, "base_upload": dict(base)}
        self.persist()

    def record_observation(self, *, step, status, observable_result,
                           evidence_kind, checks, continuity=None):
        if self.acceptance_run_id is None:
            return None
        if len(self.acceptance_observations) >= self.MAX_CHECKS:
            raise ValueError("acceptance observation limit exceeded")
        if any(row["step"] == step for row in self.acceptance_observations):
            raise ValueError("acceptance step already has an observation")
        source = self.binding.get("source", {})
        observation = build_observation(
            step=step, status=status, observable_result=observable_result,
            evidence_kind=evidence_kind, source_commit=source.get("commit"),
            run_id=self.acceptance_run_id, continuity=continuity, checks=checks,
        )
        self.acceptance_observations.append(observation)
        self.persist()
        return observation

    def acceptance_binding(self):
        return {
            "acceptance_run_id": self.acceptance_run_id,
            "acceptance_observations": list(self.acceptance_observations),
            "observed_job_ids": list(self.observed_job_ids),
            "selected_profile_id": self.selected_profile_id,
            "selected_application_id": self.selected_application_id,
            "selected_application_job_id": self.selected_application_job_id,
            "application_document_ids": list(self.application_document_ids),
            "stage_lineage": list(self.stage_lineage),
            **({"employer_discovery_lineage": self.employer_discovery_lineage}
               if self.employer_discovery_lineage is not None else {}),
            **({"cover_letter_proof": self.cover_letter_proof}
               if self.cover_letter_proof is not None else {}),
        }

    def begin(self, name):
        self.attempted = {"action": str(name)[:200], "result": "IN_PROGRESS"}
        self.persist()

    def complete(self, check):
        if len(self.checks) >= self.MAX_CHECKS:
            raise ValueError("evidence check limit exceeded")
        safe_check = {"action": str(check.get("action", "unnamed check"))[:300],
                      "result": "PASS"}
        if check.get("boundary"):
            safe_check["boundary"] = str(check["boundary"])[:500]
        self.checks.append(safe_check)
        self.attempted = None
        self.persist()

    def fail(self, error):
        stage = (self.attempted or {}).get("action", "Connected UI workflow")
        self.attempted = {"action": stage, "result": "FAIL"}
        self.failure = {"type": type(error).__name__, "stage": stage}
        if isinstance(error, MockRunTerminalError):
            self.failure["terminal_operation"] = error.terminal_summary
        self.overall_state = "FAIL"
        self.remaining_state = "NOT_RUN"
        self.persist()

    def finish(self, evidence):
        self.binding.update(evidence)
        self.overall_state = evidence.get("result", "PARTIAL")
        self.remaining_state = "NONE"
        self.attempted = None
        self.persist()

    def payload(self):
        checks = list(self.checks)
        if self.attempted:
            checks.append(dict(self.attempted))
        return {
            **self.binding,
            **self.acceptance_binding(),
            "overall_state": self.overall_state,
            "checks": checks,
            "remaining_state": self.remaining_state,
            **({"failure": self.failure} if self.failure else {}),
        }

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(self.payload(), indent=2, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > self.MAX_BYTES:
            raise ValueError("evidence size limit exceeded")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=f".{self.path.name}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def application_profile_version_matches(profile, application):
    """Keep application acceptance pinned to a current profile readback."""
    return (isinstance(profile, dict) and isinstance(application, dict)
            and isinstance(profile.get("id"), str)
            and profile.get("id") == application.get("profile_id")
            and isinstance(profile.get("version"), str)
            and bool(profile["version"])
            and profile.get("version") == application.get("profile_version"))


def prepare_ui_artifact_directory(base_url, evidence_path, acceptance_run_id=None):
    """Prepare a non-overwriting per-run artifact directory for the browser."""
    parsed_target = urlsplit(base_url)
    if acceptance_run_id is None:
        artifacts = Path("outputs/ui-e2e") / str(parsed_target.port)
        artifacts.mkdir(parents=True, exist_ok=True)
        return artifacts

    validate_run_id(acceptance_run_id)
    evidence_path = Path(evidence_path)
    run_suffix = hashlib.sha256(acceptance_run_id.encode("utf-8")).hexdigest()[:12]
    artifacts = evidence_path.parent / (evidence_path.stem + "-" + run_suffix + "-artifacts")
    if artifacts.is_symlink():
        raise FileExistsError("run-scoped artifact directory must not be a symlink")
    if artifacts.exists():
        if (not artifacts.is_dir()
                or any(artifacts.iterdir())):
            raise FileExistsError(
                "run-scoped artifact directory is not empty; refusing reuse")
    else:
        artifacts.mkdir(parents=True, exist_ok=False)
    return artifacts


def wait_for_mock_run(request, path, operation_id, *, failure_context=None,
                      failure_context_reader=None, attempt_history_reader=None, timeout_seconds=210,
                      clock=time.monotonic, sleep=time.sleep):
    """Wait for this reviewed intent, without resubmitting or hiding worker failure."""
    deadline = clock() + timeout_seconds
    while True:
        result = request(path)
        if result.get("operation_id") != operation_id:
            raise RuntimeError("mock run identity changed")
        state_value = result.get("state")
        state = state_value if isinstance(state_value, str) else "UNKNOWN"
        if state == "COMPLETED":
            return result
        if state not in {"QUEUED", "RUNNING"}:
            # Do not publish arbitrary worker diagnostics or untrusted state text.
            safe_state = state if state in {"FAILED", "CANCELLED", "BLOCKED", "DEAD_LETTERED"} else "UNKNOWN"
            attempt_history = None
            if attempt_history_reader is not None:
                try:
                    attempt_history = attempt_history_reader()
                except Exception:
                    attempt_history = {"read_state": "UNAVAILABLE"}
            terminal_context = None
            if failure_context_reader is not None:
                try:
                    terminal_context = failure_context_reader()
                except Exception:
                    terminal_context = {"read_state": "UNAVAILABLE"}
            raise MockRunTerminalError(
                operation_id=operation_id,
                state=safe_state,
                error_code=result.get("error_code"),
                context=failure_context,
                terminal_context=terminal_context,
                attempt_history=attempt_history,
            )
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError(f"mock run completion timed out: {state}")
        sleep(min(1, remaining))


def validate_onboarding_preflight(settings, health, readiness):
    if (settings.get("operation_mode") != "FIXTURE"
            or settings.get("external_submission_enabled") is not False
            or health.get("operation_mode") != "FIXTURE"
            or health.get("external_submission_enabled") is not False
            or readiness.get("status") != "ready"):
        raise RuntimeError("onboarding requires ready FIXTURE mode with external submission disabled")


def validate_onboarding_target(base_url):
    parsed = urlsplit(base_url)
    hostname = (parsed.hostname or "").lower()
    loopback = hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "http" or not loopback or parsed.username or parsed.password:
        raise RuntimeError("onboarding target must be an HTTP loopback Compose service")


def collect_onboarding_job_inventory(get_json, profile_id):
    """Capture the complete same-profile persisted inventory for restart recovery."""
    inventory = collect_persisted_job_inventory(get_json, profile_id)
    if (not isinstance(inventory, dict)
            or type(inventory.get("schema_version")) is not int
            or inventory.get("schema_version") != 4
            or inventory.get("scope") != "ALL_PERSISTED_JOBS"
            or inventory.get("profile_id") != profile_id):
        raise ValueError("Onboarding recovery requires a complete v4 inventory for this profile")
    return inventory


def assert_onboarding_profile_facts(profile, pending_fact_ids, *,
                                    expected_approved_fact_texts=frozenset({"Python", "SQL"}),
                                    expected_unapproved_fact_texts=frozenset()):
    facts = profile["facts"]
    by_id = {row["fact_id"]: row for row in facts}
    pending_fact_ids = set(pending_fact_ids)
    expected_approved_fact_texts = set(expected_approved_fact_texts)
    expected_unapproved_fact_texts = set(expected_unapproved_fact_texts)
    if expected_approved_fact_texts & expected_unapproved_fact_texts:
        raise ValueError("a profile fact cannot be expected as both approved and unapproved")
    assert len(by_id) == len(facts), "duplicate profile fact IDs"
    approved = [row for row in facts if row["verified"] and row["user_approved"]]
    if ({row["text"] for row in approved} != expected_approved_fact_texts
            or len(approved) != len(expected_approved_fact_texts)
            or any(row["kind"] != "skill" for row in approved)
            or any(row["fact_id"] in pending_fact_ids for row in approved)):
        raise AssertionError("unexpected approved facts in onboarding profile")
    expected_unapproved = [row for row in facts
                           if row["text"] in expected_unapproved_fact_texts]
    if (len(expected_unapproved) != len(expected_unapproved_fact_texts)
            or {row["text"] for row in expected_unapproved} != expected_unapproved_fact_texts
            or any(row["kind"] != "skill" or not row["verified"] or row["user_approved"]
                   or row["fact_id"] in pending_fact_ids for row in expected_unapproved)):
        raise AssertionError("expected unapproved onboarding facts changed")
    pending = [row for row in facts if row["fact_id"] in pending_fact_ids]
    if (len(pending) != len(pending_fact_ids)
            or any(row["verified"] or row["user_approved"] for row in pending)
            or set(by_id) != pending_fact_ids
            | {row["fact_id"] for row in approved + expected_unapproved}):
        raise AssertionError("onboarding profile facts differ from reviewed fact set")
    return {row["text"]: row["fact_id"] for row in approved}


def assert_no_pending_facts_in_documents(documents, pending_fact_ids, approved_fact_ids):
    pending_fact_ids = set(pending_fact_ids)
    approved_fact_ids = set(approved_fact_ids)
    for document in documents:
        used_ids = set(document.get("fact_ids", []))
        facts_used = document.get("facts_used", [])
        used_ids.update(row.get("fact_id") for row in facts_used)
        if (used_ids & pending_fact_ids or used_ids - approved_fact_ids
                or any(not row.get("verified") or not row.get("user_approved")
                       for row in facts_used)):
            raise AssertionError("pending or unsupported facts reached generated documents")


def assert_search_scoped_discovery(request_payload, operation, jobs, *, profile_id,
                                   search_profile_id, expected_titles):
    """Check queued fixture discovery and durable readback for a selected saved search."""
    if request_payload.get("profile_id") != profile_id:
        raise AssertionError("discovery request changed the active profile")
    if request_payload.get("search_profile_id") != search_profile_id:
        raise AssertionError("discovery request omitted or changed the selected saved search")
    if operation.get("entity_id") != profile_id:
        raise AssertionError("discovery operation changed the active profile")
    params = operation.get("params") or {}
    if params.get("profile_id", profile_id) != profile_id:
        raise AssertionError("discovery operation parameters changed the active profile")
    if params.get("search_profile_id") != search_profile_id:
        raise AssertionError("discovery operation did not retain the selected saved search")
    if any(row.get("profile_id", profile_id) != profile_id for row in jobs):
        raise AssertionError("search-filtered readback included another profile's job")
    titles = [row.get("title") for row in jobs]
    if set(titles) != set(expected_titles) or len(titles) != len(expected_titles):
        raise AssertionError("search-filtered job readback differs from the reviewed criteria")


def assert_discovery_request_operation_pair(request_payload, operation, *, profile_id,
                                            search_profile_id, expected_operation_id):
    """Tie a submitted request to its operation; entity_id owns the profile scope."""
    params = operation.get("params") if isinstance(operation, dict) else None
    if (not isinstance(request_payload, dict) or not isinstance(params, dict)
            or not isinstance(request_payload.get("request_id"), str)
            or not request_payload["request_id"]
            or not isinstance(operation.get("id"), str) or not operation["id"]
            or operation["id"] != expected_operation_id
            or request_payload.get("profile_id") != profile_id
            or operation.get("entity_id") != profile_id
            or request_payload.get("search_profile_id") != search_profile_id
            or params.get("search_profile_id") != search_profile_id
            or params.get("request_id") != request_payload["request_id"]):
        raise AssertionError("discovery request and persisted operation do not match")
    return operation["id"], request_payload["request_id"]


def assert_supported_fixture_search(search, *, profile_id, include_titles, remote):
    """Require a Sources-tab discovery search, never a Jobs filter snapshot."""
    criteria = search.get("criteria") if isinstance(search, dict) else None
    if not isinstance(criteria, dict):
        raise AssertionError("saved fixture search has no criteria")
    if search.get("profile_id") != profile_id or search.get("enabled") is not True:
        raise AssertionError("fixture search is disabled or belongs to another profile")
    if search.get("source_ids") != []:
        raise AssertionError("fixture discovery requires the all-sources search scope")
    if (criteria.get("include_titles") != include_titles or criteria.get("remote") != remote
            or criteria.get("employment_types") != []):
        raise AssertionError("saved fixture search differs from the reviewed Sources criteria")
    if "job_filters" in criteria:
        raise AssertionError("Jobs-tab filters cannot be used as provider discovery criteria")
    scheduler = criteria.get("scheduler")
    if not isinstance(scheduler, dict) or scheduler.get("enabled") is not False:
        raise AssertionError("fixture search must keep its background scheduler disabled")


def assert_reprepared_profile_pins(*, previous_profile_version, old_application_profile_version,
                                   current_profile_version, resumed_application_profile_version):
    """Compare opaque profile-version identities by equality, not lexical order."""
    versions = (previous_profile_version, old_application_profile_version,
                current_profile_version, resumed_application_profile_version)
    if any(not isinstance(version, str) or not version for version in versions):
        raise AssertionError("reprepare profile versions must be nonempty identities")
    if previous_profile_version == current_profile_version:
        raise AssertionError("reviewed profile edit did not produce a new profile version")
    if old_application_profile_version != previous_profile_version:
        raise AssertionError("application did not retain its old profile-version pin before reprepare")
    if resumed_application_profile_version != current_profile_version:
        raise AssertionError("reprepared application is not pinned to the approved current profile")


def assert_blocked_mock_operation(operation, *, application_id):
    """Validate the legacy public operation record used by onboarding recovery."""
    assert operation.get("kind") == "APPLY_MOCK", operation
    assert operation.get("entity_id") == application_id, operation
    assert operation.get("state") == "BLOCKED", operation


def assert_previous_review_operation(request, *, onboarding, operation_id, application_id):
    """Keep the onboarding-only blocked receipt check out of the demo flow."""
    if not onboarding:
        return
    assert operation_id, "onboarding recovery requires the prior blocked operation"
    operation = request("/operations/" + operation_id)
    assert_blocked_mock_operation(operation, application_id=application_id)


def is_allowed_onboarding_application_write(method, path):
    """Allow only the explicit local application operations used by onboarding."""
    if method != "POST":
        return False
    if path == "/applications":
        return True
    parts = path.strip("/").split("/")
    if (len(parts) == 5 and parts[0] == "profiles" and bool(parts[1])
            and parts[2] == "applications" and bool(parts[3])
            and parts[4] in {"reprepare", "mock-run"}):
        return True
    comparison_path = (len(parts) == 6 and parts[0] == "profiles" and bool(parts[1])
                       and parts[2] == "applications" and bool(parts[3])
                       and parts[4:] == ["uploaded-resume-comparison", "operations"])
    comparison_cancel_path = (len(parts) == 8 and parts[0] == "profiles" and bool(parts[1])
                              and parts[2] == "applications" and bool(parts[3])
                              and parts[4:6] == ["uploaded-resume-comparison", "operations"]
                              and bool(parts[6]) and parts[7] == "cancel")
    return comparison_path or comparison_cancel_path


def is_allowed_onboarding_request(method, url, allowed_origin):
    """Enforce same-origin, no employer submission, and exact local write routes."""
    parsed = urlsplit(url)
    path = parsed.path.lower()
    blocked_path = (path.rstrip("/").endswith(("/apply", "/submit"))
                    or path in {"/apply", "/submit"}
                    or "/external-application" in path)
    application_write = method != "GET" and "/applications/" in path
    return (parsed.netloc == allowed_origin and not blocked_path
            and (not application_write
                 or is_allowed_onboarding_application_write(method, path)))


def sanitized_onboarding_request(method, url):
    """Return method and a query-free route path with profile/application IDs hidden."""
    parts = urlsplit(url).path.strip("/").split("/")
    if len(parts) >= 4 and parts[0].lower() == "profiles" and parts[2].lower() == "applications":
        parts[1] = "<profile>"
        parts[3] = "<application>"
    path = "/" + "/".join(parts) if parts and parts != [""] else "/"
    return {"method": method.upper(), "path": path}


def expect_visible_with_onboarding_diagnostics(locator, blocked_requests, **kwargs):
    """Keep local browser failures actionable without recording request payloads."""
    try:
        expect(locator).to_be_visible(**kwargs)
    except AssertionError as error:
        if blocked_requests:
            raise AssertionError(
                f"{error}\nBlocked onboarding requests (method/path only): {blocked_requests}"
            ) from error
        raise


def answer_bank_confirmation_locator(answer_group, question_number):
    """Locate the confirmation div by its aria-label (it is not a region role)."""
    return answer_group.locator(
        f'[aria-label="Answer Bank confirmation for question {question_number}"]'
    )


def _onboarding_fixture_pdf():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    text = pdf.beginText(40, 780)
    for line in (
        "Summary", "Fictional onboarding summary for extraction review only.",
    ):
        text.textLine(line)
    pdf.drawText(text)
    pdf.save()
    return stream.getvalue()


def _onboarding_preflight(base_url):
    validate_onboarding_target(base_url)
    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
        settings = client.get("/settings")
        settings.raise_for_status()
        health = client.get("/health")
        health.raise_for_status()
        readiness = client.get("/readiness")
        readiness.raise_for_status()
    validate_onboarding_preflight(settings.json(), health.json(), readiness.json())


def wait_for_ui_action_idle(page, button_name):
    """Wait for the shared UI busy gate to release a form action."""
    expect(page.get_by_role("button", name=button_name, exact=True)).to_be_enabled()


def wait_for_fixture_collection_ui_idle(page):
    """Wait until fixture collection, its UI refresh, and the shared busy gate finish."""
    expect(page.get_by_role("status").filter(
        has_text="Local fixture jobs collected for your saved profile. No employer was contacted."
    )).to_be_visible()
    # The collection action navigates back to Jobs before action() completes its
    # final refresh/readback. This stable Jobs control remains mounted through
    # that transition and is disabled by the same busy gate as the Sources form.
    wait_for_ui_action_idle(page, "Refresh matches")


def _source_binding():
    def git_value(*args):
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=5, check=True)
        return result.stdout.strip()

    try:
        return {"commit": git_value("rev-parse", "HEAD"),
                "tree": git_value("rev-parse", "HEAD^{tree}"),
                "working_tree_diff_sha256": hashlib.sha256(
                    git_value("diff", "--binary", "HEAD").encode("utf-8")
                ).hexdigest(),
                "working_tree_status_sha256": hashlib.sha256(
                    git_value("status", "--porcelain=v1", "--untracked-files=all").encode("utf-8")
                ).hexdigest()}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "tree": None, "state": "UNAVAILABLE"}


def run(base_url, evidence_path, onboarding=False, acceptance_run_id=None,
        runtime_identity=None):
    if onboarding:
        # This read-only gate runs before output creation, Chromium launch, or any UI write.
        _onboarding_preflight(base_url)
    artifacts = prepare_ui_artifact_directory(
        base_url, evidence_path, acceptance_run_id)
    parsed_target = urlsplit(base_url)
    journal = EvidenceJournal(evidence_path, {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "ONBOARDING_FIXTURE" if onboarding else "FICTIONAL_DEMO",
        "target_origin": f"{parsed_target.scheme}://{parsed_target.netloc}",
        "source": _source_binding(),
        **({"runtime_identity": runtime_identity}
           if acceptance_run_id is not None and runtime_identity is not None else {}),
    }, acceptance_run_id=acceptance_run_id)

    def observe_acceptance_step(step, status, observable_result, *,
                                evidence_kind, checks, continuity=None):
        return journal.record_observation(
            step=step, status=status, observable_result=observable_result,
            evidence_kind=evidence_kind, checks=checks, continuity=continuity,
        )

    def record_application_step(step, observable_result, assertions, *,
                                stage_lineage=None, document_id=None):
        if acceptance_run_id is None:
            return None
        from cli.acceptance_applications import record_application_step as record

        return record(
            journal, step, observable_result=observable_result,
            assertions=assertions, runtime_identity=runtime_identity,
            stage_lineage=stage_lineage, document_id=document_id,
        )

    def _paired_evidence(ui, readback):
        from cli.acceptance_applications import AssertionEvidence

        return AssertionEvidence(ui=bool(ui), readback=bool(readback))

    def operation_lineage(response, step, *, operation_kind):
        assert response.status == 202, response.text()
        payload = response.json()
        operation_id = payload.get("operation_id")
        post_data = response.request.post_data_json or {}
        request_id = post_data.get("request_id")
        if operation_kind == "PREPARE":
            scoped_profile_id = post_data.get("profile_id")
            scoped_job_id = post_data.get("job_id")
            scoped_application_id = payload.get("application_id")
            expected_entity_id = scoped_profile_id
        else:
            scoped_profile_id = profile_id
            scoped_job_id = application["job_id"]
            scoped_application_id = identifier
            expected_path = (
                f"/profiles/{scoped_profile_id}/applications/{scoped_application_id}/mock-run")
            assert urlsplit(response.request.url).path == expected_path
            expected_entity_id = scoped_application_id
        operation = request("/operations/" + str(operation_id))
        params = operation.get("params") or {}
        assert operation.get("id") == operation_id
        if operation_kind == "APPLY_MOCK":
            scoped_readback = request(
                f"/profiles/{scoped_profile_id}/applications/{scoped_application_id}/mock-runs/"
                + str(request_id))
            assert_mock_run_request_lineage(
                operation, scoped_readback, profile_id=scoped_profile_id,
                application_id=scoped_application_id, request_id=request_id)
        else:
            assert params.get("request_id") == request_id and request_id
        assert operation.get("kind") == operation_kind
        assert operation.get("entity_id") == expected_entity_id
        assert scoped_profile_id == profile_id
        assert scoped_application_id and scoped_job_id
        return {
            "step": step, "operation_id": operation_id,
            "request_id": request_id, "operation_kind": operation_kind,
            "application_id": scoped_application_id, "profile_id": scoped_profile_id,
            "job_id": scoped_job_id,
        }, operation

    class CheckpointActions(list):
        def append(self, value):
            if value.get("result") == "PASS":
                journal.complete(value)
            super().append(value)

    actions = CheckpointActions()
    job_inventory = None
    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
        unsafe_requests = []
        if onboarding:
            allowed_origin = urlsplit(base_url).netloc

            def local_fixture_route(route):
                current = route.request
                if not is_allowed_onboarding_request(current.method, current.url, allowed_origin):
                    unsafe_requests.append(sanitized_onboarding_request(current.method, current.url))
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", local_fixture_route)
        page = context.new_page()
        page.set_default_timeout(45000)
        failures = []
        page.on("pageerror", lambda failure: failures.append(str(failure)))

        def navigation(name):
            page.get_by_role("navigation").get_by_role("button", name=name).click()

        def request(path):
            response = context.request.get(base_url + path)
            assert response.ok, (path, response.status)
            return response.json()

        def download_document(document_id, format):
            path = f"/profiles/{profile_id}/documents/{document_id}/{format}"
            response = context.request.get(base_url + path)
            assert response.ok, (path, response.status)
            return response.body()

        def save_profile():
            save_button = page.get_by_role("button", name="Save profile", exact=True)
            save_button.click()
            expect(page.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()
            # The notice is set before action() finishes its refresh and jobs readback.
            # Wait for the actual shared busy gate to clear before continuing to another form.
            wait_for_ui_action_idle(page, "Save profile")

        def fact_editor(text):
            for fieldset in page.locator("fieldset.fact").all():
                value = fieldset.get_by_role("textbox", name="Fact text", exact=True)
                if value.count() and value.input_value() == text:
                    return fieldset
            raise AssertionError("Expected exact profile fact text was not found: " + text)

        first_prepare = {"lineage": None, "operation": None}
        route_adapter_evidence = None

        def prepare():
            if page.get_by_label("Order approved skills for this job", exact=True).is_checked():
                preview = page.get_by_role("region", name="Resume keyword alignment preview", exact=True)
                preview.get_by_role("button", name="Preview keyword alignment", exact=True).click()
                expect(preview.get_by_role("status")).to_contain_text("Read-only preview")
            prepare_button = page.get_by_role("button", name="Prepare documents", exact=True)
            if (acceptance_run_id is not None and onboarding
                    and first_prepare["lineage"] is None):
                with page.expect_response(lambda response:
                        response.request.method == "POST"
                        and urlsplit(response.url).path == "/applications") as prepared:
                    prepare_button.click()
                first_prepare["lineage"], first_prepare["operation"] = operation_lineage(
                    prepared.value, 36, operation_kind="PREPARE")
            else:
                prepare_button.click()
            expect(page.get_by_role("heading", name="Application details", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Run dry-run", exact=True)).to_be_enabled()

        def select_data_job():
            navigation("Jobs")
            page.get_by_label("Search jobs").fill("data")
            if onboarding:
                page.get_by_label("Job title", exact=True).fill("")
                page.get_by_label("Work location", exact=True).select_option("")
            card = page.get_by_role("button", name="Example Systems Inc. Data Engineer", exact=True)
            expect(card).to_have_count(1)
            card.click()
            expect(page.get_by_role("heading", name="Why this match?", exact=True)).to_be_visible()

        try:
            page.goto(base_url, wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="Find your next opportunity.")).to_be_visible()
            observe_acceptance_step(
                2, "PASS", "Dashboard landing screen is visible in the browser.",
                evidence_kind="CONTROLLED_FIXTURE",
                checks=[{"name": "dashboard_heading_visible", "passed": True}],
            )
            pending_fact_ids = set()
            discovery_search_proofs = []
            first_run_initial = None
            base_resume_bytes = None
            base_resume_revision_id = None
            base_resume_sha256 = None
            employer_initial = None
            employer_first_receipt = None
            if onboarding:
                if acceptance_run_id is not None:
                    from cli.acceptance_onboarding import begin_first_run

                    first_run_initial = begin_first_run(
                        page, base_url, acceptance_run_id=acceptance_run_id)
                    if not first_run_initial.get("first_run_started"):
                        raise AssertionError("Fresh first-run profile creation was not observed.")
                    profile_id = first_run_initial["profile_id"]
                    assert page.get_by_label("Active profile", exact=True).input_value() == profile_id
                    profile = request("/profiles/" + profile_id)
                    assert profile["facts"] == []
                else:
                    page.get_by_role("button", name="New profile", exact=True).click()
                    page.get_by_label("Full name", exact=True).fill("Synthetic Onboarding Applicant")
                    page.get_by_label("Email", exact=True).fill("onboarding@example.invalid")
                    with page.expect_response(lambda response:
                            response.request.method == "PUT"
                            and urlsplit(response.url).path == "/profiles") as profile_saved:
                        save_profile()
                    assert profile_saved.value.status == 200, profile_saved.value.text()
                    profile_id = profile_saved.value.json()["id"]
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value(profile_id)
                    assert profile_id and not profile_id.startswith("demo-")
                    profile = request("/profiles/" + profile_id)
                    assert profile["facts"] == []
                    observe_acceptance_step(
                        3, "PARTIAL", "Profile created through the profile form; first-run wizard not exercised.",
                        evidence_kind="CONTROLLED_FIXTURE",
                        checks=[{"name": "new_profile_saved_and_selected", "passed": True}],
                        continuity={"profile_id": profile_id},
                    )
                actions.append({"action": "UI created a new synthetic profile without replacing existing profiles", "result": "PASS"})

                page.get_by_label("Choose a resume (up to 10 MiB)").set_input_files({
                    "name": "synthetic-onboarding.pdf", "mimeType": "application/pdf",
                    "buffer": _onboarding_fixture_pdf(),
                })
                page.get_by_label("Resume label", exact=True).fill("Synthetic onboarding fixture")
                page.get_by_role("button", name="Store resume", exact=True).click()
                resume_row = page.locator("[data-resume-version='1']")
                expect(resume_row).to_be_visible()
                resume_row.get_by_role("button", name="Extract and review", exact=True).click()
                review = page.get_by_role("region", name="Resume extraction review")
                expect(review).to_be_visible()
                suggestions = review.locator('label.fact-suggestion:has(strong) input[type="checkbox"]')
                assert suggestions.count() > 0
                for suggestion in suggestions.all():
                    expect(suggestion).not_to_be_checked()
                    suggestion.check()
                expect(review.get_by_role("button", name="Add selected to profile editor", exact=True)).to_be_enabled()
                review.get_by_role("button", name="Add selected to profile editor", exact=True).click()
                imported_rows = page.locator("fieldset.fact")
                expect(imported_rows).not_to_have_count(0, timeout=10000)
                for imported_fact in imported_rows.all():
                    expect(imported_fact).to_contain_text("Pending")
                    expect(imported_fact.get_by_label("I verified this fact", exact=True)).not_to_be_checked()
                    expect(imported_fact.get_by_label("I approve its use in documents", exact=True)).not_to_be_checked()
                save_profile()
                imported_profile = request("/profiles/" + profile_id)
                pending_fact_ids = {row["fact_id"] for row in imported_profile["facts"]}
                assert pending_fact_ids
                assert all(not row["verified"] and not row["user_approved"]
                           for row in imported_profile["facts"])
                observe_acceptance_step(
                    4, "PASS", "Synthetic resume was uploaded and extracted through the UI.",
                    evidence_kind="CONTROLLED_FIXTURE",
                    checks=[{"name": "resume_upload_and_extraction_visible", "passed": True}],
                    continuity={"profile_id": profile_id},
                )
                observe_acceptance_step(
                    5, "PASS", "Extracted suggestions remain pending until reviewed and approved.",
                    evidence_kind="CONTROLLED_FIXTURE",
                    checks=[{"name": "extracted_suggestions_remain_unapproved", "passed": True}],
                    continuity={"profile_id": profile_id},
                )

                correction = next(row for row in imported_rows.all()
                                  if row.get_by_role("textbox", name="Fact text", exact=True).count())
                corrected_fact = correction.get_by_role(
                    "textbox", name="Fact text", exact=True)
                prior_fact_text = corrected_fact.input_value()
                correction_text = prior_fact_text + " (corrected after review)"
                corrected_fact.fill(correction_text)
                expect(correction.get_by_label("I verified this fact", exact=True)).not_to_be_checked()
                expect(correction.get_by_label("I approve its use in documents", exact=True)).not_to_be_checked()
                save_profile()
                corrected_profile = request("/profiles/" + profile_id)
                corrected_row = next(row for row in corrected_profile["facts"]
                                     if row["text"] == correction_text)
                assert corrected_row["verified"] is False and corrected_row["user_approved"] is False
                pending_fact_ids = {row["fact_id"] for row in corrected_profile["facts"]}
                observe_acceptance_step(
                    6, "PASS", "An extracted fact was corrected and saved as an unapproved pending fact.",
                    evidence_kind="CONTROLLED_FIXTURE",
                    checks=[{"name": "corrected_fact_read_back", "passed": True},
                            {"name": "corrected_fact_remains_pending", "passed": True}],
                    continuity={"profile_id": profile_id},
                )
                actions.append({"action": "PDF imported through UI and extraction suggestions remain unverified and unapproved after readback", "result": "PASS"})

                for fact_text in ("Python", "SQL"):
                    page.get_by_role("button", name="Add fact", exact=True).click()
                    new_fact_editor = page.locator("fieldset.fact").last
                    new_fact_editor.get_by_role("combobox").select_option("skill")
                    new_fact_editor.get_by_label("Confidence (0–100)", exact=True).fill("100")
                    new_fact_editor.get_by_label("Fact text", exact=True).fill(fact_text)
                    new_fact_editor.get_by_label("Evidence / source", exact=True).fill("Explicitly reviewed synthetic onboarding fact")
                    new_fact_editor.get_by_label("I verified this fact", exact=True).check()
                    new_fact_editor.get_by_label("I approve its use in documents", exact=True).check()
                for question, answer_text in (
                    ("Country", "United States"),
                    ("Remote role", "yes"),
                    ("Approved fixture answer", "Controlled fictional test application."),
                ):
                    page.get_by_role("button", name="Add answer", exact=True).click()
                    answer_editor = page.locator("fieldset.fact").last
                    answer_editor.get_by_role("textbox", name="Question this answer addresses", exact=True).fill(question)
                    answer_editor.get_by_role("textbox", name="Answer text", exact=True).fill(answer_text)
                    answer_editor.get_by_label("Provenance", exact=True).fill("Explicit synthetic mock-form review")
                    answer_editor.get_by_label("Confidence (0–100)", exact=True).fill("100")
                    if question != "Approved fixture answer":
                        answer_editor.get_by_label("I reviewed and verified this answer", exact=True).check()
                        answer_editor.get_by_label("I approve exact reuse for this question", exact=True).check()
                save_profile()
                profile = request("/profiles/" + profile_id)
                assert len(profile["answers"]) == 3
                answer_by_question = {row["question"]: row for row in profile["answers"]}
                assert {"Country", "Remote role", "Approved fixture answer"} == set(answer_by_question)
                assert all(answer_by_question[question]["verified"]
                           and answer_by_question[question]["user_approved"]
                           and answer_by_question[question]["confidence"] == 100
                           for question in ("Country", "Remote role"))
                assert not answer_by_question["Approved fixture answer"]["verified"]
                assert not answer_by_question["Approved fixture answer"]["user_approved"]
                assert not {row["id"] for row in profile["answers"]} & {"country", "remote", "fixture_question"}
                actions.append({"action": "UI saves two explicitly approved synthetic answers and keeps the required mock-form answer unreviewed for later question review", "result": "PASS"})
                skill_fact_ids = assert_onboarding_profile_facts(profile, pending_fact_ids)
                python_fact_id = skill_fact_ids["Python"]
                sql_fact_id = skill_fact_ids["SQL"]
                actions.append({"action": "Only explicitly entered and reviewed Python and SQL facts are verified and approved", "result": "PASS"})
                observe_acceptance_step(
                    7, "PASS", "Only the explicitly reviewed synthetic Python and SQL facts are approved.",
                    evidence_kind="CONTROLLED_FIXTURE",
                    checks=[{"name": "approved_fact_set_matches_review", "passed": True}],
                    continuity={"profile_id": profile_id},
                )
                if acceptance_run_id is not None:
                    base_resume_bytes = b"MJ-REQ-292 synthetic base resume; these original bytes must stay unchanged.\n"
                    page.get_by_label("Document type", exact=True).select_option("BASE_RESUME")
                    page.get_by_label("Document label", exact=True).fill("MJ-REQ-292 synthetic comparison base")
                    page.get_by_label("Choose a document (PDF, DOCX or TXT; up to 10 MiB)", exact=True).set_input_files({
                        "name": "mj-req-292-base.txt", "mimeType": "text/plain",
                        "buffer": base_resume_bytes,
                    })
                    with page.expect_response(lambda response:
                            response.request.method == "POST"
                            and urlsplit(response.url).path == f"/profiles/{profile_id}/document-library") as base_stored:
                        page.get_by_role("button", name="Store document", exact=True).click()
                    assert base_stored.value.status == 201, base_stored.value.text()
                    base_library = request(f"/profiles/{profile_id}/document-library")
                    base_row = next(row for row in base_library["uploaded"]
                                    if row.get("document_kind") == "BASE_RESUME"
                                    and row.get("label") == "MJ-REQ-292 synthetic comparison base")
                    base_resume_revision_id = base_row["revision_id"]
                    base_resume_sha256 = hashlib.sha256(base_resume_bytes).hexdigest()
                    assert base_row["sha256"] == base_resume_sha256
                    expect(page.get_by_role("region", name="MJ-REQ-292 synthetic comparison base collection", exact=True)).to_be_visible()

                def save_sources_fixture_search(name, title):
                    navigation("Sources")
                    page.get_by_label("Search name", exact=True).fill(name)
                    page.get_by_label("Remote preference").select_option("REQUIRED")
                    page.get_by_label("Employment type").select_option("")
                    page.get_by_label("Include titles", exact=True).fill(title)
                    with page.expect_response(lambda candidate:
                            candidate.request.method == "PUT"
                            and urlsplit(candidate.url).path == "/search-profiles") as saved:
                        journal.begin("Save selected Sources search")
                        page.get_by_role("button", name="Save search", exact=True).click()
                    assert saved.value.status == 200, saved.value.text()
                    expect(page.get_by_role("status").filter(
                        has_text="Search saved. Its schedule is off")).to_be_visible()
                    search = next(row for row in request(
                        "/search-profiles?profile_id=" + profile_id)["search_profiles"]
                        if row["name"] == name)
                    assert_supported_fixture_search(
                        search, profile_id=profile_id, include_titles=[title], remote="REQUIRED")
                    navigation("Jobs")
                    page.get_by_label("Saved search profile", exact=True).last.select_option(search["id"])
                    expect(page.get_by_label("Saved search profile", exact=True).last).to_have_value(search["id"])
                    return search

                def collect_sources_fixture_search(search, expected_titles):
                    # Search selection happens in Jobs; the collection control is in Sources.
                    navigation("Sources")
                    with page.expect_response(lambda response:
                            response.request.method == "POST"
                            and urlsplit(response.url).path == "/discovery") as discovery_response:
                        page.get_by_role("button", name="Collect local fixture jobs", exact=True).click()
                    response = discovery_response.value
                    assert response.status == 202, response.text()
                    discovery_request = response.request.post_data_json
                    assert discovery_request["search_profile_id"] == search["id"]
                    operation_id = response.json()["operation_id"]
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        operation = request("/operations/" + operation_id)
                        if operation["state"] in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
                            break
                        time.sleep(0.5)
                    assert operation["state"] == "COMPLETED", operation
                    operation_id, request_id = assert_discovery_request_operation_pair(
                        discovery_request, operation, profile_id=profile_id,
                        search_profile_id=search["id"], expected_operation_id=operation_id)
                    jobs = request("/jobs?profile_id=" + profile_id
                                   + "&search_profile_id=" + search["id"])["jobs"]
                    assert_search_scoped_discovery(
                        discovery_request, operation, jobs,
                        profile_id=profile_id, search_profile_id=search["id"],
                        expected_titles=set(expected_titles))
                    # Operation completion and filtered readback can precede the
                    # UI action's own refresh/loadJobs and busy-gate release.
                    wait_for_fixture_collection_ui_idle(page)
                    discovery_search_proofs.append({
                        "search_profile_id": search["id"],
                        "operation_id": operation_id,
                        "request_id": request_id,
                        "profile_id": profile_id,
                        "filtered_titles": [row["title"] for row in jobs],
                        "result": "PASS",
                    })
                    return operation_id, discovery_request["request_id"], jobs

                # Create searches through the product's supported Sources form.
                # Jobs-tab filter snapshots are intentionally disabled for scheduling
                # and cannot be resumed as provider discovery criteria.
                nonmatching_search = save_sources_fixture_search(
                    "Synthetic remote IT onboarding search", "IT Support Specialist")
                if acceptance_run_id is not None:
                    from cli.acceptance_employer import begin_employer_observation

                    employer_initial = begin_employer_observation(
                        page, request, profile_id)
                discovery_operation_id, discovery_request_id, nonmatching_jobs = (
                    collect_sources_fixture_search(nonmatching_search, set()))
                if acceptance_run_id is not None:
                    employer_first_receipt = {
                        "profile_id": profile_id,
                        "search_profile_id": nonmatching_search["id"],
                        "operation_id": discovery_operation_id,
                        "request_id": discovery_request_id,
                    }
                assert {row["title"] for row in request(
                    "/jobs?profile_id=" + profile_id)["jobs"]} >= {"Python Developer", "Data Engineer"}
                actions.append({"action": "Created a remote IT title search in the Sources UI; actual fixture discovery completed and the selected same-profile filter returned no matching jobs", "result": "PASS", "boundary": "Sources search form → Jobs saved-search selection → queued /discovery → Compose source worker → selected-search jobs API"})

                matching_search = save_sources_fixture_search(
                    "Synthetic remote data engineering onboarding search", "Data Engineer")
                discovery_operation_id, discovery_request_id, matching_jobs = (
                    collect_sources_fixture_search(matching_search, {"Data Engineer"}))
                discovery_job_ids = {row["id"] for row in matching_jobs}
                assert discovery_job_ids, "matching saved search returned no fixture jobs"
                actions.append({"action": "Selected a matching remote Data Engineer search; actual fixture worker completed and only the matching job appeared in same-profile filtered readback", "result": "PASS", "boundary": "Jobs UI → Sources UI → queued /discovery → Compose source worker → selected-search jobs API"})
                history = request("/profiles/" + profile_id + "/search-history")
                assert history["profile_id"] == profile_id
                history_item = next(item for item in history["items"]
                                    if item["id"].startswith(discovery_operation_id + ":"))
                assert history_item["status"] == "COMPLETED"
                assert history_item["profile_id"] == profile_id
                assert history_item["source"] == "mock-fixture"
                assert history_item["results"] and history_item["results"] > 0
                persisted_operation = request("/operations/" + discovery_operation_id)
                assert history_item["id"] == (discovery_operation_id + ":"
                                               + str(persisted_operation["attempt_count"]))
                assert_discovery_request_operation_pair(
                    {"request_id": discovery_request_id, "profile_id": profile_id,
                     "search_profile_id": matching_search["id"]},
                    persisted_operation, profile_id=profile_id,
                    search_profile_id=matching_search["id"],
                    expected_operation_id=discovery_operation_id)
                search_after_discovery = next(row for row in request(
                    "/search-profiles?profile_id=" + profile_id)["search_profiles"]
                    if row["id"] == matching_search["id"])
                assert search_after_discovery["criteria"]["include_titles"] == ["Data Engineer"]
                assert search_after_discovery["criteria"]["remote"] == "REQUIRED"
                assert "job_filters" not in search_after_discovery["criteria"]
                assert search_after_discovery["source_ids"] == []
                it_search = next(row for row in request(
                    "/search-profiles?profile_id=" + profile_id)["search_profiles"]
                    if row["id"] == nonmatching_search["id"])
                assert it_search["criteria"]["include_titles"] == ["IT Support Specialist"]
                assert it_search["criteria"]["remote"] == "REQUIRED"
                observe_acceptance_step(
                    8, "PASS", "Remote IT and Data Engineer searches were saved through the Sources UI.",
                    evidence_kind="CONTROLLED_FIXTURE",
                    checks=[{"name": "remote_it_search_read_back", "passed": True},
                            {"name": "remote_data_search_read_back", "passed": True}],
                    continuity={"profile_id": profile_id,
                                "operation_id": discovery_operation_id,
                                "request_id": discovery_request_id},
                )
                observe_acceptance_step(
                    10, "PASS", "Fixture discovery completed and search-scoped jobs were read back.",
                    evidence_kind="CONTROLLED_FIXTURE",
                    checks=[{"name": "discovery_operation_completed", "passed": True},
                            {"name": "search_scoped_job_readback", "passed": True}],
                    continuity={"profile_id": profile_id,
                                "operation_id": discovery_operation_id,
                                "request_id": discovery_request_id},
                )
                observe_acceptance_step(
                    11, "PARTIAL", "Synthetic fixture discovery does not prove real-provider jobs.",
                    evidence_kind="CONTROLLED_FIXTURE",
                    checks=[{"name": "same_profile_public_readonly_jobs_observed", "passed": False}],
                    continuity={"profile_id": profile_id,
                                "operation_id": discovery_operation_id,
                                "request_id": discovery_request_id},
                )
                if acceptance_run_id is not None:
                    run_fixture_job_observations(
                        page, request, profile_id, navigation, observe_acceptance_step,
                        record_observed_jobs=journal.record_observed_jobs)
                    from cli.acceptance_employer import observe_employer_journey

                    employer_evidence = observe_employer_journey(
                        page, request, profile_id, navigation,
                        observe_acceptance_step, employer_initial,
                        employer_first_receipt,
                        record_observed_jobs=journal.record_observed_jobs,
                    )
                    journal.record_employer_discovery_lineage(
                        employer_evidence["employer_discovery_lineage"])
                    profile_after_job_observation = request("/profiles/" + profile_id)
                    assert profile_after_job_observation["id"] == profile_id
                    assert profile_after_job_observation["facts"] == profile["facts"]
                    assert profile_after_job_observation["answers"] == profile["answers"]
                    assert profile_after_job_observation["preferences"] == profile["preferences"]
                    profile = profile_after_job_observation
            else:
                page.get_by_role("button", name="Load fictional demo", exact=True).click()
                expect(page.get_by_role("status").filter(has_text="Fictional demo loaded")).to_be_visible()
                profile_id = page.get_by_label("Active profile").input_value()
                assert profile_id.startswith("demo-")
                profile = request("/profiles/" + profile_id)
                python_fact_id, sql_fact_id = "python", "sql"
            select_data_job()
            expect(page.locator(".big-score")).to_have_text("100% · Exceptional")
            if acceptance_run_id is not None and onboarding:
                matching = request("/jobs?profile_id=" + profile_id).get("jobs", [])
                data_job = next(row for row in matching
                                if row.get("title") == "Data Engineer")
                selected_detail = request(
                    "/jobs/" + data_job["id"] + "?profile_id=" + profile_id)
                selected_score = (selected_detail.get("match") or {}).get("score")
                strong_match = (isinstance(selected_score, (int, float))
                                and not isinstance(selected_score, bool)
                                and selected_score >= 70)
                if strong_match:
                    page.locator("section.job-detail").get_by_role(
                        "button", name="Save job", exact=True).click()
                    expect(page.locator("section.job-detail").get_by_text(
                        "Current status: SAVED", exact=True)).to_be_visible()
                tracking_readback = request(
                    "/jobs/" + data_job["id"] + "?profile_id=" + profile_id).get("tracking", {})
                saved_status = strong_match and tracking_readback.get("disposition") == "SAVED"
                observe_acceptance_step(
                    32, "PASS" if saved_status else "PARTIAL",
                    "The selected Data Engineer job was saved and read back."
                    if saved_status else "The selected Data Engineer job was not confirmed saved.",
                    evidence_kind="CONTROLLED_FIXTURE",
                    checks=[{"name": "strong_match_available", "passed": strong_match},
                            {"name": "saved_status_readback", "passed": saved_status}],
                    continuity={"profile_id": profile_id, "job_id": data_job["id"]},
                )
            actions.append({"action": "Explicit fictional demo, connected search, match explanation and raw provenance", "result": "PASS"})

            profile_before_sql_review = profile
            navigation("Profile")
            fact_editor("SQL").get_by_label("I approve its use in documents", exact=True).uncheck()
            save_profile()
            profile = request("/profiles/" + profile_id)
            assert profile["id"] == profile_id
            assert profile["preferences"] == profile_before_sql_review["preferences"]
            assert profile["answers"] == profile_before_sql_review["answers"]
            before_facts = {
                row.get("fact_id", row.get("id")): {
                    key: value for key, value in row.items() if key not in {"id", "version"}}
                for row in profile_before_sql_review["facts"]
            }
            after_facts = {
                row.get("fact_id", row.get("id")): {
                    key: value for key, value in row.items() if key not in {"id", "version"}
                }
                for row in profile["facts"]
            }
            assert set(after_facts) == set(before_facts)
            for fact_id, previous_fact in before_facts.items():
                current_fact = after_facts[fact_id]
                if fact_id == sql_fact_id:
                    assert previous_fact["user_approved"] is True
                    assert current_fact == {**previous_fact, "user_approved": False}
                else:
                    assert current_fact == previous_fact
            select_data_job()
            expect(page.locator(".big-score")).to_have_text("50% · Weak")
            prepare()
            application = request("/applications?profile_id=" + profile_id)["applications"][0]
            identifier = application["id"]
            if onboarding:
                assert application["job_id"] in discovery_job_ids, "selected application job was not returned by this profile's fixture discovery"
            assert application["fact_ids"] == [python_fact_id]
            first_documents = request("/profiles/" + profile_id + "/applications/" + identifier + "/documents")["documents"]
            journal.record_application_readback(
                profile_id=application["profile_id"], application_id=identifier,
                job_id=application["job_id"], documents=first_documents)
            if acceptance_run_id is not None and onboarding:
                assert first_prepare["lineage"] is not None
                assert first_prepare["lineage"]["application_id"] == identifier
                first_application_readback = request(
                    f"/profiles/{profile_id}/applications/{identifier}")
                assert first_application_readback["profile_id"] == profile_id
                assert first_application_readback["job_id"] == application["job_id"]
                assert first_application_readback["state"] == "PREPARED"
                assert application_profile_version_matches(profile, first_application_readback)
                assert first_application_readback["profile_version"] == profile["version"]
                record_application_step(
                    36, "The selected B application was prepared and read back with its profile and job pins.",
                    {
                        "application_prepared_readback": _paired_evidence(
                            page.get_by_role("heading", name="Application details", exact=True).is_visible(),
                            first_application_readback["state"] == "PREPARED"),
                        "profile_job_pins_match": _paired_evidence(
                            page.get_by_label("Active profile", exact=True).input_value() == profile_id,
                            first_application_readback["profile_id"] == profile_id
                            and first_application_readback["job_id"] == application["job_id"]),
                    },
                    stage_lineage=first_prepare["lineage"],
                )

                navigation("Applications")
                page.get_by_role("button", name="Example Systems Inc. Data Engineer", exact=True).click()
                expect(page.get_by_role("heading", name="Application details", exact=True)).to_be_visible()
                application_review = page.get_by_role("region", name="Application review", exact=True)
                selected_job_id = application["job_id"]
                route_readback = request(
                    f"/jobs/{selected_job_id}/routes?profile_id={profile_id}")
                route_choice = next(row for row in first_application_readback["route_choices"]
                                    if row["route_id"] == first_application_readback["route_id"])
                adapter = first_application_readback.get("adapter_knowledge") or {}
                route_inventory = [
                    route for option in route_readback.get("all_options", [])
                    for route in option.get("aliases", [])
                    if route.get("id") == route_choice["route_id"]]
                route_ui_text = application_review.locator("dd").nth(2).inner_text()
                route_ui_matches = all(value in route_ui_text for value in (
                    adapter.get("provider", ""), route_choice.get("requested_mode", ""),
                    route_choice.get("state", "")))
                record_application_step(
                    37, "The displayed application route matched the selected route readback for B.",
                    {
                        "selected_route_matches_application": _paired_evidence(
                            route_ui_matches,
                            route_choice["route_id"] == first_application_readback["route_id"]),
                        "route_bound_to_job": _paired_evidence(
                            route_ui_matches,
                            bool(route_inventory) and route_readback.get("selected")
                            and route_choice["route_id"] == first_application_readback["route_id"]),
                    },
                    stage_lineage={**first_prepare["lineage"], "step": 37},
                )
                adapter_panel = page.get_by_role(
                    "region", name="ATS application adapter knowledge", exact=True)
                route_provider = route_inventory[0].get("provider") if route_inventory else None
                adapter_matches = (adapter.get("provider") == route_provider
                                   and adapter.get("adapter_status") == "FIXTURE_ONLY")
                expected_provider_label = application_provider_label(adapter.get("provider"))
                known_provider_values = adapter_panel.locator("dl dd")
                provider_label_visible = (
                    expected_provider_label is not None
                    and known_provider_values.count() > 0
                    and known_provider_values.first.inner_text().strip() == expected_provider_label)
                route_adapter_evidence = {
                    "supported_adapter_matches_provider": _paired_evidence(
                        adapter_panel.is_visible()
                        and "Fictional local mock adapter is available" in adapter_panel.inner_text(),
                        adapter_matches),
                    "provider_matches_route": _paired_evidence(
                        provider_label_visible,
                        adapter_matches),
                }
                navigation("Jobs")
                select_data_job()
            first_resume = next(row for row in first_documents if row["kind"] == "resume")
            assert first_resume['created_at'] and first_resume['generation_model'] == 'deterministic-fact-copy-v2'
            assert [fact['text'] for fact in first_resume['facts_used']] == ['Python']
            navigation("Applications")
            page.get_by_role("button", name="Example Systems Inc. Data Engineer", exact=True).click()
            expect(page.get_by_role("heading", name="Application details", exact=True)).to_be_visible()
            provenance = page.get_by_role('region', name='resume version 1', exact=True)
            expect(provenance).to_be_visible()
            expect(provenance.get_by_text('Generation details and facts', exact=True)).to_be_visible()
            provenance.get_by_text('Generation details and facts', exact=True).click()
            expect(provenance.get_by_text('Generation method: deterministic-fact-copy-v2', exact=True)).to_be_visible()
            with page.expect_download() as text_download:
                provenance.get_by_role('link', name='Download TXT', exact=True).click()
            text_bytes = Path(text_download.value.path()).read_bytes()
            assert hashlib.sha256(text_bytes).hexdigest() == first_resume['sha256']['txt']
            original = download_document(first_resume["id"], "docx")
            text = "\n".join(row.text for row in Document(io.BytesIO(original)).paragraphs)
            assert "Python" in text and "SQL" not in text
            assert first_resume['template']=='verified-headings-v1'
            assert [p.text for p in Document(io.BytesIO(original)).paragraphs if p.style.name=='Heading 1']==['Skills']
            first_cover = next(row for row in first_documents if row["kind"] == "cover-letter")
            if first_run_initial is not None:
                assert_no_pending_facts_in_documents(
                    first_documents, pending_fact_ids, {python_fact_id})
                pending_rows = [row for row in request("/profiles/" + profile_id)["facts"]
                                if row["fact_id"] in pending_fact_ids]
                assert len(pending_rows) == len(pending_fact_ids)
                rejection_before = request(f"/profiles/{profile_id}/fact-rejections")
                rejection_states = {row["fact_id"]: row
                                    for row in rejection_before.get("facts", [])}
                assert all(rejection_states.get(row["fact_id"], {}).get("review_status") == "PENDING"
                           for row in pending_rows)
                navigation("Profile")
                for fact in pending_rows:
                    editor = fact_editor(fact["text"])
                    editor.get_by_label("I reject this fact as incorrect", exact=True).check()
                    rejection_button = editor.get_by_role(
                        "button", name="Reject this fact", exact=True)
                    expect(rejection_button).to_be_enabled()
                    fact_path = (f"/profiles/{quote(profile_id, safe='')}/facts/"
                                 f"{quote(fact['fact_id'], safe='')}/reject")
                    with page.expect_response(lambda response:
                            response.request.method == "POST"
                            and urlsplit(response.url).path == fact_path) as rejected:
                        rejection_button.click()
                    assert rejected.value.status == 200, rejected.value.text()
                    receipt = rejected.value.json()
                    assert receipt["profile_id"] == profile_id
                    assert receipt["fact_id"] == fact["fact_id"]
                    assert receipt["decision"] == "REJECT"
                    expect(editor.get_by_text(
                        "Rejected — original fact retained and excluded from documents.",
                        exact=True)).to_be_visible()
                rejection_after = request(f"/profiles/{profile_id}/fact-rejections")
                rejected_rows = {row["fact_id"]: row
                                 for row in rejection_after.get("facts", [])}
                assert all(rejected_rows.get(fact_id, {}).get("review_status") == "REJECTED"
                           for fact_id in pending_fact_ids)
                profile_after_rejection = request("/profiles/" + profile_id)
                assert_onboarding_profile_facts(
                    profile_after_rejection, pending_fact_ids,
                    expected_approved_fact_texts={"Python"},
                    expected_unapproved_fact_texts={"SQL"})
                assert {row["fact_id"] for row in profile_after_rejection["facts"]
                        if not row["verified"] and not row["user_approved"]} == pending_fact_ids
                actions.append({
                    "action": "Imported pending facts were read back as excluded from generated documents, then explicitly rejected per fact in the UI",
                    "result": "PASS",
                })
            actions.append({'action':'ATS resume templates expose conventional headings in the browser-downloaded DOCX while preserving approved wording','result':'PASS'})
            assert text_bytes.decode('utf-8').strip() == text
            actions.append({'action':'Original approved facts, generation metadata and checksum-verified TXT download visible through the browser', 'result':'PASS'})
            actions.append({"action": "Pending fact excluded from matching and downloaded factual DOCX", "result": "PASS"})

            navigation("Profile")
            fact_editor("SQL").get_by_label("I approve its use in documents", exact=True).check()
            save_profile()
            select_data_job()
            expect(page.locator(".big-score")).to_have_text("100% · Exceptional")
            prepare()
            revised = request("/profiles/" + profile_id + "/applications/" + identifier)
            assert revised["document_version"] == 2 and set(revised["fact_ids"]) == {python_fact_id, sql_fact_id}
            documents = request("/profiles/" + profile_id + "/applications/" + identifier + "/documents")["documents"]
            journal.record_application_readback(
                profile_id=application["profile_id"], application_id=identifier,
                job_id=application["job_id"], documents=documents)
            assert len(documents) == 4
            old = download_document(first_resume["id"], "docx")
            assert old == original
            with page.expect_download() as download:
                page.get_by_role("link", name="Download PDF", exact=True).first.click()
            pdf_path = artifacts / "fictional-document.pdf"
            download.value.save_as(pdf_path)
            assert pdf_path.read_bytes().startswith(b"%PDF")
            assert hashlib.sha256(pdf_path.read_bytes()).hexdigest() in {doc["sha256"]["pdf"] for doc in documents}
            actions.append({"action": "Approved profile revision generates new documents, preserves original bytes, and PDF downloads through UI", "result": "PASS"})

            select_data_job()
            page.get_by_label('Order approved skills for this job',exact=True).check()
            prepare()
            tailored=next(row for row in request('/profiles/'+profile_id+'/applications/'+identifier+'/documents')['documents'] if row['version']==3 and row['kind']=='resume')
            assert tailored['generation_model']=='deterministic-target-relevant-bullets-summary-skill-responsibility-order-v4'
            assert tailored['template']=='approved-target-relevant-bullets-summary-skill-responsibility-and-bullet-emphasis-v5'
            assert tailored['generation_context']['emphasized_fact_ids']
            expect(page.get_by_role('region',name='resume version 3',exact=True).get_by_text('Job-matched approved facts in bold: 2.',exact=False)).to_be_visible()
            emphasized_doc=Document(io.BytesIO(download_document(tailored['id'],'docx')))
            assert any(p.text=='Python' and all(r.bold for r in p.runs) for p in emphasized_doc.paragraphs)
            actions.append({'action':'Job-matched technology emphasis reaches worker, DOCX download and visible provenance','result':'PASS'})
            assert tailored['generation_context']['mode']=='APPROVED_SKILL_ORDER'
            assert set(tailored['fact_ids'])=={python_fact_id,sql_fact_id}
            expect(page.get_by_role('region',name='resume version 3',exact=True).get_by_text('Tailored resume: skill order',exact=False)).to_be_visible()
            actions.append({'action':'Explicit skill-order opt-in reaches the document worker and preserves approved facts with target snapshot and separate revision','result':'PASS'})

            comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
            comparison.get_by_label('Base resume',exact=True).select_option(label='v2')
            comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
            result=comparison.get_by_role('region',name='Resume comparison result',exact=True)
            comparison_documents=request('/profiles/'+profile_id+'/applications/'+identifier+'/documents')['documents']
            journal.record_application_readback(
                profile_id=application['profile_id'], application_id=identifier,
                job_id=application['job_id'], documents=comparison_documents)
            base_document=next(row for row in comparison_documents
                               if row['version']==2 and row['kind']=='resume')
            tailored_document=next(row for row in comparison_documents
                                   if row['version']==3 and row['kind']=='resume')
            expect(result.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('No content omitted.')
            expect(result.get_by_role('region',name='Summary changes',exact=True)).to_contain_text('No changes.')
            expect(result.get_by_role('heading',name='Generated base · v2',exact=True)).to_be_visible()
            expect(result.get_by_role('heading',name='Tailored resume · v3',exact=True)).to_be_visible()
            observe_acceptance_step(
                34, "PASS", "The UI compared immutable generated base and tailored resume versions.",
                evidence_kind="CONTROLLED_FIXTURE",
                checks=[{"name": "generated_base_visible", "passed": True},
                        {"name": "tailored_version_visible", "passed": True},
                        {"name": "omission_and_change_summary_visible", "passed": True}],
                continuity={"profile_id": profile_id, "application_id": identifier,
                            "job_id": application["job_id"],
                            "document_ids": [base_document["id"], tailored_document["id"]]},
            )
            actions.append({'action':'Immutable generated base and tailored resume comparison shows recorded versions and unchanged facts through the UI','result':'PASS'})

            profile_before_comparison=request('/profiles/'+profile_id)
            uploaded_original_text=b'Summary\nOriginal synthetic summary\nSkills\nSQL; Python\nResponsibilities\nMaintained synthetic servers\n'
            uploaded_response=context.request.post(base_url+'/profiles/'+profile_id+'/resumes', data=uploaded_original_text,
                headers={'X-Filename':'comparison-base.txt','Content-Type':'text/plain'})
            assert uploaded_response.status==201, uploaded_response.text()
            uploaded_original=uploaded_response.json()
            comparison.get_by_role('button',name='Refresh base resumes',exact=True).click()
            comparison.get_by_label('Base resume',exact=True).select_option('u:'+uploaded_original['revision_id'])
            comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
            expect_visible_with_onboarding_diagnostics(
                result.get_by_role('heading',name='Uploaded base · v1',exact=True), unsafe_requests)
            expect(result.get_by_role('region',name='Summary changes',exact=True)).to_contain_text('Original synthetic summary')
            expect(result.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('Maintained synthetic servers')
            expect(result.get_by_role('region',name='Original extraction provenance',exact=True)).to_contain_text('Native extracted text')
            with page.expect_download() as original_download:
                result.get_by_role('link',name='Download compared original',exact=True).click()
            assert Path(original_download.value.path()).read_bytes()==uploaded_original_text
            assert request('/profiles/'+profile_id)==profile_before_comparison
            actions.append({'action':'Uploaded original comparison preserves revision/hash and profile facts, displays changed summary and omitted content, and downloads exact source bytes','result':'PASS'})

            select_data_job()
            page.get_by_label('Order approved skills for this job',exact=True).check()
            page.get_by_label('Choose resume facts',exact=True).check()
            expect(page.get_by_label('Include SQL',exact=True)).to_be_checked()
            page.get_by_label('Include SQL',exact=True).uncheck()
            prepare()
            selected_documents=request('/profiles/'+profile_id+'/applications/'+identifier+'/documents')['documents']
            journal.record_application_readback(
                profile_id=application['profile_id'], application_id=identifier,
                job_id=application['job_id'], documents=selected_documents)
            selected_resume=next(row for row in selected_documents if row['version']==4 and row['kind']=='resume')
            selected_cover=next(row for row in selected_documents if row['version']==4 and row['kind']=='cover-letter')
            assert selected_resume['generation_model']=='deterministic-fact-selection-and-bullet-emphasis-v1'
            assert selected_resume['fact_ids']==[python_fact_id] and set(selected_cover['fact_ids'])=={python_fact_id,sql_fact_id}
            assert selected_resume['generation_context']['omitted_fact_ids']==[sql_fact_id]
            selected_history=page.get_by_role('region',name='resume version 4',exact=True)
            expect(selected_history.get_by_text('Tailored resume: selected facts',exact=False)).to_be_visible()
            with page.expect_download() as selected_download:
                selected_history.get_by_role('link',name='Download TXT',exact=True).click()
            selected_text=Path(selected_download.value.path()).read_text(encoding='utf-8')
            assert 'Python' in selected_text and 'SQL' not in selected_text
            assert 'SQL' in download_document(selected_cover['id'],'txt').decode('utf-8')
            comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
            comparison.get_by_label('Base resume',exact=True).select_option(label='v2')
            comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
            expect(comparison.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('skill: SQL')
            assert request('/profiles/'+profile_id)==profile_before_comparison
            observe_acceptance_step(
                33, "PASS", "Tailored resume contains only selected approved facts and preserves approved cover-letter facts.",
                evidence_kind="CONTROLLED_FIXTURE",
                checks=[{"name": "tailored_resume_uses_selected_approved_facts", "passed": True},
                        {"name": "omitted_fact_visible_and_excluded", "passed": True}],
                continuity={"profile_id": profile_id, "application_id": identifier,
                            "job_id": application["job_id"],
                            "document_ids": [selected_resume["id"], selected_cover["id"]]},
            )
            actions.append({'action':'Explicit approved resume selection reaches the real document worker, omits SQL only from the resume, preserves profile and cover letter, and shows immutable omissions','result':'PASS'})

            navigation('Profile')
            page.get_by_label('city',exact=True).fill('Tulsa')
            save_profile()
            saved_contact_profile=request('/profiles/'+profile_id)
            select_data_job()
            page.get_by_label('Order approved skills for this job',exact=True).uncheck()
            contact_option=page.get_by_label('Include saved resume contact details',exact=True)
            expect(contact_option).not_to_be_checked();contact_option.check()
            prepare()
            latest_b_documents=request('/profiles/'+profile_id+'/applications/'+identifier+'/documents')['documents']
            journal.record_application_readback(
                profile_id=application['profile_id'], application_id=identifier,
                job_id=application['job_id'], documents=latest_b_documents)
            contact_resume=next(row for row in latest_b_documents if row['version']==5 and row['kind']=='resume')
            assert contact_resume['template']=='approved-contact-base-v1'
            assert 'Location: Tulsa' in contact_resume['generation_context']['contact_header']
            contact_history=page.get_by_role('region',name='resume version 5',exact=True)
            contact_history.get_by_text('Generation details and facts',exact=True).click()
            expect(contact_history.get_by_role('region',name='Saved resume contact header')).to_contain_text('Location: Tulsa')
            with page.expect_download() as contact_download:
                contact_history.get_by_role('link',name='Download TXT',exact=True).click()
            assert 'Location: Tulsa' in Path(contact_download.value.path()).read_text(encoding='utf-8')
            assert 'Location: Tulsa' not in download_document(selected_resume['id'],'txt').decode('utf-8')
            assert request('/profiles/'+profile_id)==saved_contact_profile
            actions.append({'action':'Explicit saved contact header reaches real document worker, history and download while prior revision and profile remain unchanged','result':'PASS'})

            if acceptance_run_id is not None and onboarding:
                assert base_resume_bytes and base_resume_revision_id and base_resume_sha256
                a_job=next(row for row in request('/jobs?profile_id='+profile_id)['jobs']
                           if row.get('title')=='Python Developer')
                b_job=request('/jobs/'+application['job_id']+'?profile_id='+profile_id)
                a_job_detail=request('/jobs/'+a_job['id']+'?profile_id='+profile_id)
                navigation('Jobs')
                page.get_by_label('Search jobs').fill('python')
                page.get_by_label('Job title',exact=True).fill('')
                page.get_by_label('Work location',exact=True).select_option('')
                a_card=page.get_by_role('button',name='Example Systems Inc. Python Developer',exact=True)
                expect(a_card).to_be_visible()
                a_card.click()
                expect(page.locator('section.job-detail').get_by_role(
                    'heading',name='Python Developer',exact=True)).to_be_visible()
                prepare()
                a_application=next(row for row in request('/applications?profile_id='+profile_id)['applications']
                                   if row.get('job_id')==a_job['id'])
                a_documents=request('/profiles/'+profile_id+'/applications/'+a_application['id']+'/documents')['documents']
                a_cover=next(row for row in a_documents if row['kind']=='cover-letter')
                b_cover=next(row for row in latest_b_documents
                             if row['kind']=='cover-letter' and row['version']==5)
                assert a_application['id']!=identifier and a_job['id']!=application['job_id']
                cases={
                    'A': {'application_id':a_application['id'],'job_id':a_job['id'],
                          'title':a_job_detail['title'],'company':a_job_detail['company'],
                          'document_id':a_cover['id'],'version':a_cover['version'],
                          'fact_ids':a_cover['fact_ids']},
                    'B': {'application_id':identifier,'job_id':application['job_id'],
                          'title':b_job['title'],'company':b_job['company'],
                          'document_id':b_cover['id'],'version':b_cover['version'],
                          'fact_ids':b_cover['fact_ids']},
                }
                current_profile=request('/profiles/'+profile_id)
                approved_text={row['fact_id']:row['text'] for row in current_profile['facts']
                               if row.get('verified') and row.get('user_approved')}
                approved_by_case={label:tuple(approved_text[fact_id]
                    for fact_id in case['fact_ids']) for label,case in cases.items()}
                pending_sentinels=tuple(row['text'] for row in current_profile['facts']
                                        if row['fact_id'] in pending_fact_ids)

                def get_artifact_bytes(path):
                    response=context.request.get(base_url+path)
                    assert response.ok,(path,response.status)
                    return response.body()

                from cli.acceptance_documents import prove_unique_cover_letter_artifacts

                document_proof=prove_unique_cover_letter_artifacts(
                    request,get_artifact_bytes,profile_id=profile_id,cases=cases,
                    base_revision_id=base_resume_revision_id,
                    base_sha256=base_resume_sha256,base_bytes=base_resume_bytes,
                    approved_by_case=approved_by_case,pending_sentinels=pending_sentinels,
                )
                journal.record_cover_letter_proof(document_proof)
                navigation('Applications')
                page.get_by_role('button',name='Example Systems Inc. Data Engineer',exact=True).click()
                expect(page.get_by_role('heading',name='Application details',exact=True)).to_be_visible()
                restored_b=request('/profiles/'+profile_id+'/applications/'+identifier)
                assert restored_b['job_id']==application['job_id']
                assert page.get_by_label('Active profile',exact=True).input_value()==profile_id
                assert document_proof['B']['document_id']==b_cover['id']
                observe_acceptance_step(
                    35,'PASS','Distinct job-specific cover letters match the persisted A and B application records.',
                    evidence_kind='CONTROLLED_FIXTURE',
                    checks=[{'name':'cover_letter_generated','passed':True},
                            {'name':'cross_job_uniqueness_compared','passed':
                             document_proof['A']['sha256']['txt']!=document_proof['B']['sha256']['txt']}],
                    continuity={'profile_id':profile_id,'application_id':identifier,
                                'job_id':application['job_id'],'document_id':b_cover['id']},
                )
                actions.append({'action':'Real UI/API-generated Python Developer A and Data Engineer B cover letters were compared by persisted IDs and checked file hashes; the selected B application was restored','result':'PASS'})

            if onboarding:
                # Keep the uncertain-answer recovery journey specific to the onboarding fixture.
                previous_operation_id = None
                initial_mock_run_path = f"/profiles/{profile_id}/applications/{identifier}/mock-run"
                with page.expect_response(lambda response:
                        response.request.method == "POST"
                        and response.url == base_url + initial_mock_run_path) as paused_attempt:
                    page.get_by_role("button", name="Run dry-run", exact=True).click()
                paused_lineage, paused_operation = operation_lineage(
                    paused_attempt.value, 44, operation_kind="APPLY_MOCK")
                expect(page.get_by_role("status", name="Application state", exact=True)).to_have_text(
                    "REVIEW_REQUIRED", timeout=30000)
                preflight = page.get_by_role("region", name="ATS question preflight", exact=True)
                previous_profile = request("/profiles/" + profile_id)
                paused_application = request(
                    f"/profiles/{profile_id}/applications/{identifier}")
                question_preflight = paused_application.get("question_preflight")
                fixture_question = current_missing_question_by_id(
                    question_preflight, "fixture_question")
                assert fixture_question.get("question") == "Approved fixture answer"
                assert "fixture_question" in fixture_question.get("answer_ids", [])
                unresolved_list = preflight.get_by_role(
                    "list", name="Unresolved required questions", exact=True)
                expect(unresolved_list).to_contain_text(fixture_question["question"])
                review_row = exact_primary_question_row(
                    unresolved_list, fixture_question["question"])
                unresolved_prompt = review_row.locator(":scope > strong").inner_text().strip()
                assert unresolved_prompt == fixture_question["question"]
                paused_receipt = request(
                    f"/profiles/{profile_id}/applications/{identifier}/mock-runs/"
                    + paused_lineage["request_id"])
                assert paused_receipt["operation_id"] == paused_lineage["operation_id"]
                assert paused_receipt["state"] == "BLOCKED"
                assert paused_receipt["error_code"] == "REVIEW_REQUIRED"
                assert "SUBMIT" not in paused_application.get("trace", [])
                mock_run_controls = page.get_by_role(
                    "region", name="Recoverable mock run", exact=True)
                expect(mock_run_controls).to_be_visible()
                expect(mock_run_controls).to_contain_text(paused_lineage["request_id"])
                expect(mock_run_controls).to_contain_text(paused_lineage["operation_id"])
                submit_control = mock_run_controls.get_by_role(
                    "button", name="Submit to local mock", exact=True)
                submit_control_gated = submit_control_is_absent_or_disabled(submit_control)
                if acceptance_run_id is not None and route_adapter_evidence is not None:
                    record_application_step(
                        38, "The selected route and fixture-only adapter were rechecked during the local attempt.",
                        route_adapter_evidence,
                        stage_lineage={**paused_lineage, "step": 38},
                    )
                record_application_step(
                    44, "The uncertain required answer paused the local attempt before any submit step.",
                    {
                        "uncertain_question_paused": _paired_evidence(
                            page.get_by_role("status", name="Application state", exact=True).inner_text()
                            == "REVIEW_REQUIRED",
                            paused_application["question_preflight"]["is_current"] is True
                            and paused_receipt["error_code"] == "REVIEW_REQUIRED"),
                        "submit_not_attempted": _paired_evidence(
                            page.get_by_role("heading", name="Application details", exact=True).is_visible()
                            and page.get_by_role("status", name="Application state", exact=True).inner_text()
                            == "REVIEW_REQUIRED"
                            and submit_control_gated,
                            "SUBMIT" not in paused_application.get("trace", [])
                            and paused_receipt["state"] == "BLOCKED"
                            and paused_operation.get("params", {}).get("submit") is False),
                    },
                    stage_lineage=paused_lineage,
                )
                review_row.get_by_role("button", name="Review this question", exact=True).click()
                answer_group = page.get_by_role("region", name="Application answers", exact=True).get_by_role(
                    "group", name="Question 1")
                answer_group.get_by_role("textbox", name="Answer", exact=True).fill(
                    REVIEWED_FIXTURE_ANSWER)
                answer_group.get_by_label("I reviewed this exact answer for this job", exact=True).check()
                answer_group.get_by_role("button", name="Save to Answer Bank", exact=True).click()
                answer_confirmation = answer_bank_confirmation_locator(answer_group, 1)
                answer_confirmation.get_by_label(
                    "I confirm this answer is accurate and may be reused", exact=True).check()
                approval_checked_in_ui = answer_confirmation.get_by_label(
                    "I confirm this answer is accurate and may be reused", exact=True).is_checked()
                with page.expect_response(lambda response:
                        response.request.method == "PUT"
                        and urlsplit(response.url).path == "/profiles") as reviewed_answer_save:
                    answer_confirmation.get_by_role(
                        "button", name="Confirm and save to Answer Bank", exact=True).click()
                assert reviewed_answer_save.value.status == 200, reviewed_answer_save.value.text()
                updated_profile = request("/profiles/" + profile_id)
                reviewed_answer = next(row for row in updated_profile["answers"]
                                       if row["question"] == "Approved fixture answer")
                assert reviewed_answer["text"] == REVIEWED_FIXTURE_ANSWER
                assert reviewed_answer["verified"] and reviewed_answer["user_approved"]
                record_application_step(
                    45, "The user confirmed an answer for the paused required question and persisted it in the profile.",
                    {
                        "explicit_answer_approval_persisted": _paired_evidence(
                            approval_checked_in_ui and reviewed_answer_save.value.status == 200,
                            reviewed_answer["verified"] is True
                            and reviewed_answer["user_approved"] is True
                            and reviewed_answer["text"] == REVIEWED_FIXTURE_ANSWER),
                        "approval_bound_to_question": _paired_evidence(
                            unresolved_prompt == reviewed_answer["question"],
                            paused_operation.get("entity_id") == identifier
                            and reviewed_answer["question"] == unresolved_prompt),
                    },
                    stage_lineage={**paused_lineage, "step": 45},
                )
                old_pins = request("/profiles/" + profile_id + "/applications/" + identifier)
                assert old_pins["profile_version"] == previous_profile["version"]
                old_document_version = old_pins["document_version"]
                with page.expect_response(lambda candidate:
                        candidate.request.method == "POST"
                        and urlsplit(candidate.url).path.endswith("/reprepare")) as reprepare_response:
                    page.get_by_role("button", name="Resume and prepare current revision", exact=True).click()
                assert reprepare_response.value.status == 200, reprepare_response.value.text()
                expect(page.get_by_role("status").filter(
                    has_text="Run a local dry run to create a fresh ATS question preflight")).to_be_visible()
                resumed = request("/profiles/" + profile_id + "/applications/" + identifier)
                assert_reprepared_profile_pins(
                    previous_profile_version=previous_profile["version"],
                    old_application_profile_version=old_pins["profile_version"],
                    current_profile_version=updated_profile["version"],
                    resumed_application_profile_version=resumed["profile_version"],
                )
                assert resumed["document_version"] == old_document_version + 1
                assert resumed["id"] == identifier and resumed["profile_id"] == profile_id
                revised_documents = request("/profiles/" + profile_id + "/applications/" + identifier + "/documents")["documents"]
                assert any(row["version"] == old_document_version for row in revised_documents)
                assert any(row["version"] == resumed["document_version"]
                           and row["profile_version"] == updated_profile["version"]
                           for row in revised_documents)
                assert resumed.get("question_preflight") is not None
                assert resumed["question_preflight"]["is_current"] is False
                actions.append({"action": "Unknown required answer stopped dry-run; explicit question review changed approved profile facts, then explicit Resume prepared a new immutable document revision and retained old history", "result": "PASS", "boundary": "Connected React UI → public profile/reprepare APIs → actual document worker"})

                if first_run_initial is not None:
                    latest_profile = request("/profiles/" + profile_id)
                    assert latest_profile["version"] == updated_profile["version"]
                    assert_no_pending_facts_in_documents(
                        revised_documents, pending_fact_ids, {python_fact_id, sql_fact_id})
                    latest_pending = [row for row in latest_profile["facts"]
                                      if row["fact_id"] in pending_fact_ids]
                    assert len(latest_pending) == len(pending_fact_ids)
                    assert all(not row["verified"] and not row["user_approved"]
                               for row in latest_pending)
                    navigation("Profile")
                    for fact in latest_pending:
                        editor = fact_editor(fact["text"])
                        editor.get_by_label(
                            "I reject this fact as incorrect", exact=True).check()
                        reject_button = editor.get_by_role(
                            "button", name="Reject this fact", exact=True)
                        expect(reject_button).to_be_enabled()
                        fact_path = (f"/profiles/{quote(profile_id, safe='')}/facts/"
                                     f"{quote(fact['fact_id'], safe='')}/reject")
                        with page.expect_response(lambda response:
                                response.request.method == "POST"
                                and urlsplit(response.url).path == fact_path) as rejected_again:
                            reject_button.click()
                        assert rejected_again.value.status == 200
                        expect(editor.get_by_text(
                            "Rejected — original fact retained and excluded from documents.",
                            exact=True)).to_be_visible()
                    latest_reviews = request(f"/profiles/{profile_id}/fact-rejections")
                    latest_review_rows = {row["fact_id"]: row
                                          for row in latest_reviews.get("facts", [])}
                    assert all(latest_review_rows.get(fact_id, {}).get("review_status")
                               == "REJECTED" for fact_id in pending_fact_ids)
                    after_latest_rejections = request("/profiles/" + profile_id)
                    assert after_latest_rejections["version"] == latest_profile["version"]
                    assert_onboarding_profile_facts(after_latest_rejections, pending_fact_ids)
                    from cli.acceptance_onboarding import observe_first_run

                    first_run_report = observe_first_run(
                        page, base_url, first_run_initial,
                        search_profile_id=matching_search["id"],
                        operation_id=discovery_operation_id,
                        request_id=discovery_request_id,
                    )
                    observe_acceptance_step(
                        3, first_run_report["result"],
                        "First-run configuration, saved search, and current-version fact decisions were reloaded.",
                        evidence_kind="CONTROLLED_FIXTURE",
                        checks=first_run_report["checks"],
                        continuity={"profile_id": profile_id},
                    )
                    actions.append({
                        "action": "At the current profile version, each pending extracted fact was explicitly rejected and the first-run wizard was reloaded",
                        "result": "PASS" if first_run_report["result"] == "PASS" else "PARTIAL",
                    })
                    navigation("Applications")
                    page.get_by_role(
                        "button", name="Example Systems Inc. Data Engineer", exact=True).click()
                    expect(page.get_by_role(
                        "heading", name="Application details", exact=True)).to_be_visible()

            if onboarding:
                mock_controls = page.get_by_role("region", name="Recoverable mock run", exact=True)
                run_storage_key = f"mjobs-mock-run-v1:{profile_id}:{identifier}"
                previous_intent = page.evaluate(
                    "key => {const saved=localStorage.getItem(key);return saved?JSON.parse(saved):null}",
                    run_storage_key,
                )
                assert previous_intent and previous_intent["submit"] is False, (
                    "The initial review-blocked dry-run intent was not preserved for explicit resolution")
                previous_receipt = request(
                    f"/profiles/{profile_id}/applications/{identifier}/mock-runs/"
                    + previous_intent["requestId"])
                assert previous_receipt["state"] == "BLOCKED"
                assert previous_receipt["error_code"] == "REVIEW_REQUIRED"
                previous_operation_id = previous_receipt["operation_id"]
                previous_operation = request("/operations/" + previous_operation_id)
                assert_blocked_mock_operation(previous_operation, application_id=identifier)
                assert "operation_type" not in previous_operation, previous_operation
                assert resumed["question_preflight"]["is_current"] is False

                # Reprepare preserves the old terminal intent and durable receipt.
                # Require the UI's explicit user action before another run can start.
                new_run = mock_controls.get_by_role(
                    "button", name="Start a new reviewed run", exact=True)
                expect(new_run).to_be_enabled()
                new_run.click()
                assert page.evaluate("key => localStorage.getItem(key)", run_storage_key) is None
                assert request("/operations/" + previous_operation_id)["state"] == "BLOCKED"
                assert request(
                    f"/profiles/{profile_id}/applications/{identifier}/mock-runs/"
                    + previous_intent["requestId"])["operation_id"] == previous_operation_id
                expect(mock_controls.get_by_role("button", name="Run dry-run", exact=True)).to_be_enabled()
                expect(mock_controls.get_by_role("button", name="Submit to local mock", exact=True)).to_be_disabled()
                actions.append({"action": "Explicit new reviewed run clears only the prior terminal browser intent; blocked operation and stale preflight remain readable", "result": "PASS", "boundary": "Connected React UI → public run receipt/operation APIs"})

            dry_run_lineage = None
            if acceptance_run_id is not None and onboarding:
                with page.expect_response(lambda response:
                        response.request.method == "POST"
                        and response.url == base_url +
                        f"/profiles/{profile_id}/applications/{identifier}/mock-run") as dry_run_admission:
                    page.get_by_role("button", name="Run dry-run", exact=True).click()
                dry_run_lineage, dry_run_operation = operation_lineage(
                    dry_run_admission.value, 46, operation_kind="APPLY_MOCK")
            else:
                page.get_by_role("button", name="Run dry-run", exact=True).click()
            expect(page.get_by_role("status", name="Application state", exact=True)).to_have_text(
                "DRY_RUN_COMPLETE", timeout=30000)
            dry = request("/profiles/" + profile_id + "/applications/" + identifier)
            assert "STOP_BEFORE_SUBMIT" in dry["trace"] and "SUBMIT" not in dry["trace"]
            assert dry["question_preflight"]["result"] == "PASS"
            assert dry["question_preflight"]["is_current"] is True
            if dry_run_lineage is not None:
                dry_receipt = request(
                    f"/profiles/{profile_id}/applications/{identifier}/mock-runs/"
                    + dry_run_lineage["request_id"])
                assert dry_receipt["operation_id"] == dry_run_lineage["operation_id"]
                assert dry_receipt["state"] == "COMPLETED"
                dry_documents = request(
                    f"/profiles/{profile_id}/applications/{identifier}/documents")["documents"]
                journal.record_application_readback(
                    profile_id=profile_id, application_id=identifier,
                    job_id=application["job_id"], documents=dry_documents)
                history_path = (
                    f"/profiles/{profile_id}/applications/{identifier}/attempt-history")
                history = request(history_path)
                expected_attempts = [
                    row for row in history["attempts"]
                    if isinstance(row.get("native_fixture"), dict)
                    and row["native_fixture"].get("request_id")
                    == dry_run_lineage["operation_id"]]
                native_attempt = expected_attempts[0] if len(expected_attempts) == 1 else {}
                native_proof = native_attempt.get("native_fixture") or {}
                verified_uploads = native_attempt.get("verified_uploads") or []
                current_profile = request("/profiles/" + profile_id)
                from services.applications.owned_ats_fixture import _digest as native_digest

                profile_answers = {row["id"]: row for row in current_profile["answers"]}
                reviewed_fields = native_proof.get("reviewed_fields") or []
                reviewed_matches = (
                    len(reviewed_fields) == 3
                    and {row.get("field_key") for row in reviewed_fields}
                    == {"country", "remote", "fixture_question"}
                    and all(
                        row.get("answer_id") in profile_answers
                        and profile_answers[row["answer_id"]].get("question")
                        == {"country": "Country", "remote": "Remote role",
                            "fixture_question": "Approved fixture answer"}[row["field_key"]]
                        and profile_answers[row["answer_id"]].get("verified") is True
                        and profile_answers[row["answer_id"]].get("user_approved") is True
                        and row.get("answer_sha256")
                        == native_digest(profile_answers[row["answer_id"]])
                        for row in reviewed_fields))
                history_ui = page.get_by_role(
                    "region", name="Durable attempt history", exact=True)
                expect(history_ui).to_be_visible()
                native_review_ui = history_ui.get_by_role(
                    "region", name="Controlled local fixture adapter", exact=True)
                refresh_history = history_ui.get_by_role(
                    "button", name="Refresh attempt history", exact=True)
                if refresh_history.is_enabled():
                    refresh_history.click()
                history_ui_text = history_ui.inner_text()
                native_steps = set(native_attempt.get("steps", []))
                native_scope_ok = (
                    native_attempt.get("application_id") == identifier
                    and native_attempt.get("profile_version") == current_profile["version"]
                    and native_proof.get("job_id") == application["job_id"]
                    and native_proof.get("request_id") == dry_run_lineage["operation_id"])
                profile_digest_matches = (
                    native_proof.get("profile_sha256") == native_digest(current_profile))
                record_application_step(
                    39, "The local browser attempt navigated to and detected the selected native fixture form.",
                    {
                        "playwright_form_opened": _paired_evidence(
                            "NAVIGATE:COMPLETED" in history_ui_text
                            and "DETECT_FORM:COMPLETED" in history_ui_text,
                            native_scope_ok and "NAVIGATE:COMPLETED" in native_steps
                            and "DETECT_FORM:COMPLETED" in native_steps),
                        "selected_application_loaded": _paired_evidence(
                            page.get_by_role("heading", name="Application details", exact=True).is_visible(),
                            native_attempt.get("application_id") == identifier
                            and native_proof.get("job_id") == application["job_id"]),
                    },
                    stage_lineage={**dry_run_lineage, "step": 39},
                )
                def upload_matches_document(kind):
                    selected_doc = next((row for row in dry_documents
                                         if row.get("kind") == kind
                                         and row.get("version") == dry["document_version"]), None)
                    upload = next((row for row in verified_uploads
                                   if row.get("kind") == kind), None)
                    if selected_doc is None or upload is None:
                        return selected_doc, upload, False
                    if upload.get("document_id") != selected_doc.get("id"):
                        return selected_doc, upload, False
                    for file_format, expected_sha in selected_doc.get("sha256", {}).items():
                        try:
                            content = download_document(selected_doc["id"], file_format)
                        except (AssertionError, KeyError, ValueError):
                            continue
                        if (hashlib.sha256(content).hexdigest() == upload.get("sha256")
                                and len(content) == upload.get("size_bytes")):
                            return selected_doc, upload, True
                    return selected_doc, upload, False

                resume_doc, resume_upload, resume_upload_ok = upload_matches_document("resume")
                cover_doc, cover_upload, cover_upload_ok = upload_matches_document("cover-letter")
                terminal_native_validation = (
                    native_scope_ok and dry.get("state") in {"DRY_RUN_COMPLETE", "CONFIRMED"}
                    and "VALIDATE:COMPLETED" in native_steps
                    and resume_upload_ok and cover_upload_ok)
                record_application_step(
                    40, "The native attempt filled and read back contact fields from the current profile.",
                    {
                        "contact_fields_match_verified_profile": _paired_evidence(
                            "FILL_CONTACT:COMPLETED" in history_ui_text,
                            native_scope_ok and profile_digest_matches
                            and "FILL_CONTACT:COMPLETED" in native_steps),
                        "contact_fields_readback_verified": _paired_evidence(
                            "FILL_CONTACT:COMPLETED" in history_ui_text,
                            native_scope_ok and profile_digest_matches
                            and "FILL_CONTACT:COMPLETED" in native_steps
                            and terminal_native_validation),
                    },
                    stage_lineage={**dry_run_lineage, "step": 40},
                )
                record_application_step(
                    41, "The verified browser resume bytes matched the selected persisted B resume document.",
                    {
                        "resume_upload_matches_document": _paired_evidence(
                            resume_upload_ok and resume_upload["sha256"] in history_ui_text,
                            resume_upload_ok),
                        "resume_sha256_matches": _paired_evidence(
                            resume_upload_ok and resume_upload["sha256"] in history_ui_text,
                            resume_upload_ok),
                    },
                    stage_lineage={**dry_run_lineage, "step": 41},
                    document_id=resume_doc.get("id") if resume_doc else None,
                )
                record_application_step(
                    42, "The verified browser cover-letter bytes matched the selected persisted B cover letter.",
                    {
                        "cover_letter_upload_matches_document": _paired_evidence(
                            cover_upload_ok and cover_upload["sha256"] in history_ui_text,
                            cover_upload_ok),
                        "cover_letter_sha256_matches": _paired_evidence(
                            cover_upload_ok and cover_upload["sha256"] in history_ui_text,
                            cover_upload_ok),
                    },
                    stage_lineage={**dry_run_lineage, "step": 42},
                    document_id=cover_doc.get("id") if cover_doc else None,
                )
                record_application_step(
                    43, "The native attempt filled and validated the exact reviewed answers; values stay hidden.",
                    {
                        "approved_answers_match_questions": _paired_evidence(
                            native_review_ui.is_visible()
                            and "Exact reviewed fields: 3" in native_review_ui.inner_text(),
                            reviewed_matches),
                        "approved_answers_readback_verified": _paired_evidence(
                            "VALIDATE:COMPLETED" in history_ui_text,
                            reviewed_matches and terminal_native_validation),
                    },
                    stage_lineage={**dry_run_lineage, "step": 43},
                )
                record_application_step(
                    46, "The reviewed application used a current reprepare after stale preflight evidence was rejected.",
                    {
                        "reviewed_application_resumed": _paired_evidence(
                            page.get_by_role("status", name="Application state", exact=True).inner_text()
                            == "DRY_RUN_COMPLETE",
                            resumed["id"] == identifier
                            and resumed["profile_id"] == profile_id
                            and dry["profile_version"] == current_profile["version"]),
                        "stale_approval_rejected": _paired_evidence(
                            resumed["question_preflight"]["is_current"] is False,
                            resumed["question_preflight"]["is_current"] is False
                            and dry["question_preflight"]["is_current"] is True),
                    },
                    stage_lineage=dry_run_lineage,
                )
                record_application_step(
                    47, "The current form preflight and generated documents were pinned to the selected B profile revision.",
                    {
                        "current_form_validated": _paired_evidence(
                            page.get_by_role("status", name="Application state", exact=True).inner_text()
                            == "DRY_RUN_COMPLETE",
                            dry["question_preflight"]["result"] == "PASS"
                            and dry["question_preflight"]["is_current"] is True),
                        "profile_document_pins_match": _paired_evidence(
                            page.get_by_text(f"Document revision {dry['document_version']}", exact=False).is_visible(),
                            dry["profile_version"] == current_profile["version"]
                            and all(row.get("profile_version") == current_profile["version"]
                                    for row in dry_documents
                                    if row.get("version") == dry["document_version"])),
                    },
                    stage_lineage={**dry_run_lineage, "step": 47},
                )
                record_application_step(
                    48, "The dry-run receipt and attempt trace were persisted without an external post.",
                    {
                        "dry_run_receipt_persisted": _paired_evidence(
                            page.get_by_role("status", name="Application state", exact=True).inner_text()
                            == "DRY_RUN_COMPLETE",
                            dry_receipt["state"] == "COMPLETED"
                            and dry_receipt["operation_id"] == dry_run_lineage["operation_id"]),
                        "no_external_post_recorded": _paired_evidence(
                            "SUBMIT" not in history_ui_text,
                            "SUBMIT" not in dry.get("trace", [])
                            and request("/health")["external_submission_enabled"] is False),
                    },
                    stage_lineage={**dry_run_lineage, "step": 48},
                )
            assert_previous_review_operation(
                request, onboarding=onboarding,
                operation_id=previous_operation_id if onboarding else None,
                application_id=identifier if onboarding else None)
            page.get_by_role("button", name="Start a new reviewed run", exact=True).click()
            page.get_by_label("I approve one fictional mock submission").check()
            mock_run_path = f"/profiles/{profile_id}/applications/{identifier}/mock-run"
            with page.expect_response(lambda response: response.request.method == "POST"
                                      and response.url == base_url + mock_run_path) as admission:
                journal.begin("Submit to local mock")
                page.get_by_role("button", name="Submit to local mock", exact=True).click()
            assert admission.value.status == 202
            admitted = admission.value.json()
            if acceptance_run_id is not None and onboarding:
                final_lineage, admitted_operation = operation_lineage(
                    admission.value, 49, operation_kind="APPLY_MOCK")
            admitted_intent = page.evaluate("([profile, application]) => JSON.parse(localStorage.getItem('mjobs-mock-run-v1:' + encodeURIComponent(profile) + ':' + encodeURIComponent(application)))", [profile_id, identifier])
            assert admitted_intent["submit"] is True
            failure_context = {
                "application_id": identifier,
                "request_id": admitted_intent.get("requestId"),
                "expected_profile_version": dry.get("profile_version"),
                "expected_document_version": dry.get("document_version"),
                "preflight_result": (dry.get("question_preflight") or {}).get("result"),
                "preflight_is_current": (dry.get("question_preflight") or {}).get("is_current"),
                "submit": admitted_intent.get("submit"),
            }

            def read_current_failure_context():
                current = request(f"/profiles/{profile_id}/applications/{identifier}")
                return _terminal_application_context(
                    current, profile_id=profile_id, application_id=identifier)

            wait_for_mock_run(
                request,
                mock_run_path + "s/" + admitted_intent["requestId"],
                admitted["operation_id"],
                failure_context=failure_context,
                failure_context_reader=read_current_failure_context,
                attempt_history_reader=(lambda: request(
                    f"/profiles/{profile_id}/applications/{identifier}/attempt-history"))
                if onboarding else None,
            )
            expect(page.get_by_role("status", name="Application state", exact=True)).to_have_text("CONFIRMED")
            confirmed = request("/profiles/" + profile_id + "/applications/" + identifier)
            assert confirmed["target"] == "MOCK" and confirmed["confirmation"]["submission_count"] == 1
            assert request("/health")["external_submission_enabled"] is False
            controls = page.get_by_role('region', name='Recoverable mock run', exact=True)
            expect(controls.get_by_role('status').filter(has_text='Operation')).to_contain_text('COMPLETED')
            if acceptance_run_id is not None and onboarding:
                final_operation_readback = request(
                    "/operations/" + final_lineage["operation_id"])
                final_history = request(
                    f"/profiles/{profile_id}/applications/{identifier}/attempt-history")
                final_attempts = [row for row in final_history["attempts"]
                                  if isinstance(row.get("native_fixture"), dict)
                                  and row["native_fixture"].get("request_id")
                                  == final_lineage["operation_id"]]
                final_native_attempt = final_attempts[0] if len(final_attempts) == 1 else {}
                confirmation = confirmed.get("confirmation") or {}
                local_owned_run = (
                    confirmed["target"] == "MOCK"
                    and final_operation_readback.get("kind") == "APPLY_MOCK"
                    and final_operation_readback.get("state") == "COMPLETED"
                    and final_operation_readback.get("entity_id") == identifier
                    and request("/health")["external_submission_enabled"] is False)
                record_application_step(
                    49, "The owned local mock received one explicitly approved submission operation.",
                    {
                        "owned_mock_received_submit": _paired_evidence(
                            controls.get_by_role("status").filter(has_text="Operation").is_visible()
                            and page.get_by_role("status", name="Application state", exact=True).inner_text()
                            == "CONFIRMED",
                            local_owned_run and admitted_intent["submit"] is True),
                        "external_submission_absent": _paired_evidence(
                            "Submit to local mock" in controls.inner_text(),
                            request("/health")["external_submission_enabled"] is False
                            and confirmed["target"] == "MOCK"),
                    },
                    stage_lineage=final_lineage,
                )
                native_confirmation_valid = (
                    confirmation.get("native_provider") in {"greenhouse", "lever"}
                    and confirmation.get("request_id") == final_lineage["operation_id"]
                    and confirmation.get("submission_count") == 1
                    and all(isinstance(confirmation.get(key), str)
                            and re.fullmatch(r"[a-f0-9]{64}", confirmation[key])
                            for key in ("contract_sha256", "review_sha256",
                                        "resume_sha256", "cover_letter_sha256")))
                record_application_step(
                    50, "A scoped fixture confirmation was reconciled once to the selected B application.",
                    {
                        "authoritative_confirmation_detected": _paired_evidence(
                            page.get_by_role("status", name="Application state", exact=True).inner_text()
                            == "CONFIRMED",
                            native_confirmation_valid and final_native_attempt.get("state") == "CONFIRMED"),
                        "confirmation_count_one": _paired_evidence(
                            page.locator("pre").filter(
                                has_text='"submission_count": 1').is_visible(),
                            confirmation.get("submission_count") == 1
                            and confirmed.get("confirmation", {}).get("submission_count") == 1),
                    },
                    stage_lineage={**final_lineage, "step": 50},
                )
            intent = page.evaluate("([profile, application]) => JSON.parse(localStorage.getItem('mjobs-mock-run-v1:'+encodeURIComponent(profile)+':'+encodeURIComponent(application)))", [profile_id, identifier])
            assert intent['submit'] is True
            repeated_response = context.request.post(base_url + "/profiles/" + profile_id + "/applications/" + identifier + "/mock-run", data={"submit": True, "request_id": intent['requestId']})
            assert repeated_response.status == 202
            repeated = repeated_response.json()
            assert repeated["state"] == "COMPLETED"
            assert repeated['replayed'] is True
            assert request("/profiles/" + profile_id + "/applications/" + identifier)["confirmation"] == confirmed["confirmation"]
            if acceptance_run_id is not None and onboarding:
                repeated_receipt = request(
                    f"/profiles/{profile_id}/applications/{identifier}/mock-runs/"
                    + intent["requestId"])
                record_application_step(
                    51, "Repeating the same local request returned its completed receipt without a second submission.",
                    {
                        "history_receipt_persisted": _paired_evidence(
                            repeated["state"] == "COMPLETED",
                            repeated_receipt["state"] == "COMPLETED"
                            and repeated_receipt["operation_id"] == final_lineage["operation_id"]),
                        "repeated_readback_idempotent": _paired_evidence(
                            repeated["replayed"] is True,
                            repeated["operation_id"] == final_lineage["operation_id"]
                            and repeated_receipt.get("replayed") is True
                            and request("/profiles/" + profile_id + "/applications/" + identifier)
                            ["confirmation"] == confirmed["confirmation"]),
                    },
                    stage_lineage={**final_lineage, "step": 51},
                )
            capture_redacted_screenshot(page, artifacts / "application-confirmed.png")
            actions.append({"action": "Browser dry-run stops before submission; explicit local mock receipt count remains one on repeat", "result": "PASS"})

            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_label("Active profile")).to_have_value(profile_id)
            navigation("Applications")
            page.get_by_role("button", name="Example Systems Inc. Data Engineer", exact=True).click()
            expect(page.get_by_role("status", name="Application state", exact=True)).to_have_text("CONFIRMED")
            if acceptance_run_id is not None and onboarding:
                tracking_ui = page.get_by_role(
                    "region", name="Application tracking", exact=True)
                tracking_ui.get_by_label("Manual application status", exact=True).select_option("READY")
                tracking_path = (
                    f"/profiles/{profile_id}/applications/{identifier}/tracking/status")
                with page.expect_response(lambda response:
                        response.request.method == "PUT"
                        and urlsplit(response.url).path == tracking_path) as tracking_saved:
                    tracking_ui.get_by_role("button", name="Update status", exact=True).click()
                assert tracking_saved.value.status == 200
                tracking_before_reload = tracking_saved.value.json()
                tracking_readback = request(
                    f"/profiles/{profile_id}/applications/{identifier}/tracking")
                assert tracking_readback["status"] == "READY"
                assert tracking_readback["history"]
                assert tracking_readback["history"][-1]["status"] == "READY"
                expect(tracking_ui.get_by_role(
                    "list", name="Application history", exact=True)).to_contain_text("Ready")
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_label("Active profile")).to_have_value(profile_id)
                navigation("Applications")
                page.get_by_role(
                    "button", name="Example Systems Inc. Data Engineer", exact=True).click()
                expect(page.get_by_role(
                    "status", name="Application state", exact=True)).to_have_text("CONFIRMED")
                tracking_ui = page.get_by_role(
                    "region", name="Application tracking", exact=True)
                expect(tracking_ui.get_by_label(
                    "Manual application status", exact=True)).to_have_value("READY")
                tracking_after_reload = request(
                    f"/profiles/{profile_id}/applications/{identifier}/tracking")
                assert tracking_after_reload == tracking_readback
                expect(tracking_ui.get_by_role(
                    "list", name="Application history", exact=True)).to_contain_text("Ready")
                record_application_step(
                    52, "The selected B application tracking status persisted through a browser reload.",
                    {
                        "tracker_matches_application": _paired_evidence(
                            tracking_ui.get_by_label(
                                "Manual application status", exact=True).input_value() == "READY",
                            tracking_readback["status"] == "READY"
                            and tracking_after_reload["status"] == "READY"),
                        "tracker_survives_reload": _paired_evidence(
                            "Ready" in tracking_ui.get_by_role(
                                "list", name="Application history", exact=True).inner_text(),
                            tracking_after_reload == tracking_before_reload),
                    },
                    stage_lineage={**final_lineage, "step": 52},
                )
                audit_history = request(f"/profiles/{profile_id}/audit-history")
                scoped_audit_events = [event for event in audit_history["events"]
                                       if event.get("application_id") == identifier]
                event_types = {event.get("type") for event in scoped_audit_events}
                audit_ui = None
                navigation("Activity")
                audit_ui = page.get_by_role("region", name="Audit history", exact=True)
                expect(audit_ui).to_contain_text("Data Engineer")
                has_scoped_audit_events = bool(scoped_audit_events)
                exact_public_event_shape = has_scoped_audit_events and all(
                    set(event) == {"id", "type", "summary", "subject", "company",
                                   "timestamp", "application_id"}
                    for event in scoped_audit_events)
                no_answer_values = has_scoped_audit_events and all(
                    REVIEWED_FIXTURE_ANSWER not in value
                    and "onboarding@example.invalid" not in value
                    for event in scoped_audit_events for value in event.values()
                    if isinstance(value, str))
                sequence_complete = {
                    "APPLICATION_SUBMITTED", "APPLICATION_CONFIRMED",
                    "APPLICATION_STATUS_REPORTED"} <= event_types
                record_application_step(
                    53, "The B audit timeline shows submission, confirmation and reported tracking with redacted values.",
                    {
                        "audit_sequence_complete": _paired_evidence(
                            all(value in audit_ui.inner_text() for value in (
                                "Application submitted", "confirmed", "status reported")),
                            sequence_complete),
                        "audit_scope_matches_application": _paired_evidence(
                            "Data Engineer" in audit_ui.inner_text()
                            and "Example Systems Inc." in audit_ui.inner_text(),
                            len(scoped_audit_events) >= 3
                            and all(event["application_id"] == identifier
                                    and event["subject"] == "Data Engineer"
                                    and event["company"] == "Example Systems Inc."
                                    for event in scoped_audit_events)),
                        "audit_values_redacted": _paired_evidence(
                            REVIEWED_FIXTURE_ANSWER not in audit_ui.inner_text()
                            and "onboarding@example.invalid" not in audit_ui.inner_text(),
                            exact_public_event_shape and no_answer_values),
                    },
                    stage_lineage={**final_lineage, "step": 53},
                )
            navigation('Profile')
            base_resumes_before_library=request('/profiles/'+profile_id+'/resumes')['resumes']
            library=page.get_by_role('region', name='Document library', exact=True)
            library.get_by_label('Document type', exact=True).select_option('CERTIFICATION')
            library.get_by_label('Document label', exact=True).fill('Synthetic certificate')
            library.get_by_label('Choose a document', exact=False).set_input_files(
                {'name':'synthetic-certificate.txt','mimeType':'text/plain','buffer':b'Synthetic library proof, not a career credential.'})
            library.get_by_role('button', name='Store document', exact=True).click()
            collection=library.get_by_role('region',name='Synthetic certificate collection',exact=True)
            expect(collection.get_by_role('link',name='Download original version 1',exact=True)).to_be_visible()
            stored=request('/profiles/'+profile_id+'/document-library')['uploaded']
            certificate=next(row for row in stored if row['document_kind']=='CERTIFICATION')
            with page.expect_download() as download:
                collection.get_by_role('link',name='Download original version 1',exact=True).click()
            assert hashlib.sha256(Path(download.value.path()).read_bytes()).hexdigest()==certificate['sha256']
            assert request('/profiles/'+profile_id+'/resumes')['resumes']==base_resumes_before_library
            if onboarding:
                assert_onboarding_profile_facts(request('/profiles/'+profile_id), pending_fact_ids)
                assert_no_pending_facts_in_documents(
                    request('/profiles/'+profile_id+'/applications/'+identifier+'/documents')['documents'],
                    pending_fact_ids, {python_fact_id, sql_fact_id})
            else:
                assert {row['fact_id'] for row in request('/profiles/'+profile_id)['facts']}=={'python','sql'}
            actions.append({'action':'Typed document library stores/downloads checksum-backed certification without adding resume-import entries or career facts','result':'PASS'})
            navigation("Activity")
            expect(page.get_by_role("heading", name="Service health", exact=True)).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"
            capture_redacted_screenshot(page, artifacts / "activity-mobile.png")
            assert not failures, failures
            assert not unsafe_requests, unsafe_requests
            actions.append({"action": "Browser reload restores selected profile and application history; responsive Activity has no horizontal overflow or browser exceptions", "result": "PASS"})
            def read_bytes(path):
                response = context.request.get(base_url + path)
                assert response.ok, 'Persistence download failed'
                return response.body()
            sources = request('/company-sources')['company_sources']
            assert sources, 'Persistence proof requires a seeded company source'
            pinned = context.request.put(base_url + '/company-sources/' + sources[0]['id'] + '/pin',
                                         data={'pinned': True})
            assert pinned.ok, 'Synthetic company source control did not persist'
            assert next(row for row in request('/company-sources')['company_sources']
                        if row['id'] == sources[0]['id'])['priority'] == 'USER_PINNED'
            local_persistence = persistence.collect(request, read_bytes, profile_id, identifier)
            if onboarding:
                # Capture the complete persisted same-profile jobs/source-copy
                # baseline while the Playwright API request context is open. The
                # connected recovery workflow compares this exact inventory after restart.
                job_inventory = collect_onboarding_job_inventory(request, profile_id)
        except BaseException as error:
            try:
                journal.fail(error)
            except Exception:
                # Evidence I/O must not replace the UI failure being diagnosed.
                pass
            try:
                capture_redacted_screenshot(page, artifacts / "failure.png")
            except Exception:
                # The primary UI exception remains the failure reported to the caller.
                pass
            raise
        finally:
            try:
                journal.persist()
            except Exception:
                if sys.exc_info()[0] is None:
                    raise
            context.close()
            browser.close()
    evidence = {"result": "PARTIAL" if onboarding else "PASS", "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": "python -m cli.ui_e2e --base-url " + base_url + (" --onboarding" if onboarding else ""), "target": "Connected React UI / PostgreSQL / three RQ queues / actual Chromium",
        "profile_id": profile_id, "application_id": identifier, "operation_id": repeated["operation_id"], "request_id": intent['requestId'], "confirmation": confirmed["confirmation"], "external_submission_enabled": False,
        "mode": "ONBOARDING_FIXTURE" if onboarding else "FICTIONAL_DEMO",
        "local_persistence": local_persistence,
        **({"job_inventory": job_inventory} if onboarding else {}),
        "discovery_operation_id": discovery_operation_id if onboarding else None,
        "discovery_request_id": discovery_request_id if onboarding else None,
        "discovery_search_proofs": discovery_search_proofs if onboarding else [],
        "limitations": ([
            "This onboarding journey uses synthetic fixture jobs and does not prove real-provider browser discovery (criterion 11).",
            "Source identity/deduplication and live public-source proof are outside this local fixture run.",
        ] if onboarding else []),
        "actions": actions,
        **journal.acceptance_binding(),
        "artifacts": [str(path) for path in artifacts.glob("*") if path.name != "failure.png"]}
    journal.finish(evidence)
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18700")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--onboarding", action="store_true",
                        help="Run the connected UI acceptance from a new synthetic profile in ready FIXTURE mode")
    parser.add_argument("--acceptance-run-id",
                        help="Attach numbered UI acceptance observations to the matching runner invocation")
    options = parser.parse_args()
    run(options.base_url, options.evidence, onboarding=options.onboarding,
        acceptance_run_id=options.acceptance_run_id)
