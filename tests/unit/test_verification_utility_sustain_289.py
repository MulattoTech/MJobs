"""A connected verifier must not accept a PASS without check evidence."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from cli.verification_checks import new_report, validate_report


def current_evidence(tmp_path):
    expected = new_report("current-invocation", "revision", "source-hash")
    expected["workspace"] = str(tmp_path / "fixture")
    report = deepcopy(expected)
    report.update(
        runtime={"pid": 123, "python": sys.version.split()[0]},
        finished_at=datetime.now(timezone.utc).isoformat(),
        observations={},
    )
    return expected, report


@pytest.mark.parametrize(
    "check_name",
    [
        "Browser worker healthy",
        "duplicate-submission protection",
    ],
)
def test_unsubstantiated_single_check_pass_is_rejected(check_name, tmp_path):
    expected, report = current_evidence(tmp_path)
    report["checks"][check_name] = {
        "status": "PASS",
        "reason": "the child asserted success without producing check evidence",
    }

    with pytest.raises(ValueError):
        validate_report(report, expected, pid=123)


def test_browser_pass_is_accepted_with_successful_no_submit_browser_flow(tmp_path):
    expected, report = current_evidence(tmp_path)
    for check_name in ("Browser worker healthy", "Dry-run mode functional", "mock application fill"):
        report["checks"][check_name] = {"status": "PASS", "reason": "executed fixture evidence"}
    report["observations"]["dry_run_submission_count"] = 0

    validate_report(report, expected, pid=123)


def test_browser_pass_with_submit_activity_is_rejected(tmp_path):
    expected, report = current_evidence(tmp_path)
    for check_name in ("Browser worker healthy", "Dry-run mode functional", "mock application fill"):
        report["checks"][check_name] = {"status": "PASS", "reason": "executed fixture evidence"}
    report["observations"]["dry_run_submission_count"] = 1

    with pytest.raises(ValueError):
        validate_report(report, expected, pid=123)


@pytest.mark.parametrize(
    "missing_evidence",
    ["Dry-run mode functional", "mock application fill", "dry_run_submission_count"],
)
def test_browser_pass_requires_each_supporting_evidence(tmp_path, missing_evidence):
    expected, report = current_evidence(tmp_path)
    for check_name in ("Browser worker healthy", "Dry-run mode functional", "mock application fill"):
        report["checks"][check_name] = {"status": "PASS", "reason": "executed fixture evidence"}
    report["observations"]["dry_run_submission_count"] = 0

    if missing_evidence in report["checks"]:
        report["checks"][missing_evidence] = {"status": "NOT_RUN", "reason": "not executed"}
    else:
        del report["observations"][missing_evidence]

    with pytest.raises(ValueError, match="browser execution evidence missing"):
        validate_report(report, expected, pid=123)


@pytest.mark.parametrize("submission_count", [False, "0", 0.0])
def test_browser_pass_rejects_non_integer_zero_submission_counts(tmp_path, submission_count):
    expected, report = current_evidence(tmp_path)
    for check_name in ("Browser worker healthy", "Dry-run mode functional", "mock application fill"):
        report["checks"][check_name] = {"status": "PASS", "reason": "executed fixture evidence"}
    report["observations"]["dry_run_submission_count"] = submission_count

    with pytest.raises(ValueError, match="browser execution evidence missing"):
        validate_report(report, expected, pid=123)


def duplicate_receipt_evidence(tmp_path, submission_count):
    expected, report = current_evidence(tmp_path)
    workspace = Path(expected["workspace"])
    workspace.mkdir()
    app_id = "fixture-application"
    receipt = "fixture-receipt"
    with sqlite3.connect(workspace / "fixture.db") as db:
        db.execute("CREATE TABLE job_postings (id INTEGER)")
        db.executemany("INSERT INTO job_postings VALUES (?)", [(1,), (2,), (3,), (4,)])
        db.execute(
            "CREATE TABLE applications (id TEXT, profile_id TEXT, state TEXT, confirmation TEXT)"
        )
        db.execute(
            "INSERT INTO applications VALUES (?, ?, ?, ?)",
            (app_id, "verification-current-invocation", "CONFIRMED", json.dumps({"receipt": receipt})),
        )
    (workspace / "receipts.json").write_text(
        json.dumps({app_id: {"submission_count": submission_count, "receipt": receipt}}), encoding="utf-8"
    )
    report["checks"]["adapter registry"] = {"status": "PASS", "reason": "executed fixture evidence"}
    report["checks"]["mock global source"] = {"status": "PASS", "reason": "executed fixture evidence"}
    report["checks"]["mock confirmation"] = {"status": "PASS", "reason": "executed fixture evidence"}
    report["checks"]["duplicate-submission protection"] = {
        "status": "PASS", "reason": "executed fixture evidence",
    }
    report["observations"].update(
        registry_requests=["/v1/boards/fixture", "/v1/boards/fixture/jobs"],
        registry_company="Synthetic employer",
        registry_job_requisition="288",
        registry_provider="greenhouse",
        registry_tenant="fixture",
        profile_id="verification-current-invocation",
        canonical_jobs=2,
        submission_count=submission_count,
        confirmation_operation_id="operation-original",
        duplicate_retry_operation_id="operation-original",
        duplicate_retry_confirmation_matches=True,
        duplicate_retry_submission_count=1,
    )
    return expected, report


@pytest.mark.parametrize(
    ("submission_count", "missing_check", "accepted"),
    [
        (1, None, True),
        (2, None, False),
        (1, "mock confirmation", False),
        (1, "mock global source", False),
    ],
)
def test_duplicate_protection_pass_requires_durable_receipt_and_prerequisites(
    tmp_path, submission_count, missing_check, accepted
):
    expected, report = duplicate_receipt_evidence(tmp_path, submission_count)
    if missing_check:
        report["checks"][missing_check] = {"status": "NOT_RUN", "reason": "not executed"}

    if accepted:
        validate_report(report, expected, pid=123)
    else:
        with pytest.raises(ValueError):
            validate_report(report, expected, pid=123)


@pytest.mark.parametrize(
    "missing_evidence",
    [
        "confirmation_operation_id",
        "duplicate_retry_operation_id",
        "duplicate_retry_confirmation_matches",
        "duplicate_retry_submission_count",
    ],
)
def test_duplicate_protection_pass_requires_observed_retry_proof(
    tmp_path, missing_evidence
):
    expected, report = duplicate_receipt_evidence(tmp_path, submission_count=1)
    del report["observations"][missing_evidence]

    with pytest.raises(ValueError, match="duplicate replay evidence missing"):
        validate_report(report, expected, pid=123)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("duplicate_retry_operation_id", "different-operation"),
        ("duplicate_retry_confirmation_matches", False),
        ("duplicate_retry_submission_count", 0),
        ("duplicate_retry_submission_count", 2),
        ("duplicate_retry_submission_count", True),
    ],
)
def test_duplicate_protection_pass_rejects_invalid_retry_proof(
    tmp_path, field, invalid_value
):
    expected, report = duplicate_receipt_evidence(tmp_path, submission_count=1)
    report["observations"][field] = invalid_value

    with pytest.raises(ValueError, match="duplicate replay evidence missing"):
        validate_report(report, expected, pid=123)
