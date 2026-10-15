"""Reject incomplete, stale and forged connected child evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import subprocess
import sys

import pytest

from cli.verification_checks import (
    CHECKS, exit_status, new_report, run_connected, validate_connected_timeout, validate_report,
)


def current_report():
    expected = new_report("current-invocation", "revision", "input-hash")
    result = deepcopy(expected)
    result.update(runtime={"pid": 123, "python": sys.version.split()[0]},
                  finished_at=datetime.now(timezone.utc).isoformat())
    return expected, result


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "120.1"])
def test_connected_execution_cannot_request_unbounded_wait(value):
    with pytest.raises(ValueError):
        validate_connected_timeout(value)


@pytest.mark.parametrize("key,value", [
    ("invocation_id", "older-run"), ("revision", "older-head"),
    ("source_sha256", "older-inputs"), ("external_submission_enabled", True),
    ("finished_at", (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()),
    ("runtime", {"pid": 999, "python": sys.version.split()[0]}),
    ("checks", {"mock confirmation": {"status": "PASS", "reason": "old evidence"}}),
])
def test_rejects_forged_or_stale_current_evidence(key, value):
    expected, result = current_report()
    result[key] = value
    with pytest.raises(ValueError):
        validate_report(result, expected, pid=123)


@pytest.mark.parametrize("value", [True, "ready", "PARTIAL", "pass", None])
def test_only_explicit_outcome_enum_is_accepted(value):
    expected, result = current_report()
    result["checks"][CHECKS[0]]["status"] = value
    with pytest.raises(ValueError):
        validate_report(result, expected, pid=123)


def test_all_pass_json_without_executed_side_effects_is_rejected():
    expected, result = current_report()
    for row in result["checks"].values():
        row.update(status="PASS", reason="forged successful flag")
    with pytest.raises(ValueError):
        validate_report(result, expected, pid=123)


def test_partial_and_blocked_reports_have_nonzero_exit():
    _, result = current_report()
    assert exit_status(result) == 2
    result["checks"][CHECKS[0]]["status"] = "FAIL"
    assert exit_status(result) == 1
    result["checks"][CHECKS[0]]["status"] = "BLOCKED"
    assert exit_status(result) == 1


def test_no_fixture_admission_cannot_launch_or_connect(monkeypatch):
    monkeypatch.setattr("cli.verification_checks.source_identity", lambda: ("revision", "inputs"))

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe request launched fixture execution")

    monkeypatch.setattr("cli.verification_checks.OwnedProcess", forbidden)
    report = run_connected(fixture=False)
    assert exit_status(report) == 1
    assert not any(row["status"] == "PASS" for row in report["checks"].values())


def test_unavailable_containment_is_blocked_without_uncontained_fallback(monkeypatch):
    monkeypatch.setattr("cli.verification_checks.source_identity", lambda: ("revision", "inputs"))
    monkeypatch.setattr("cli.verification_process.sys.platform", "unsupported-platform")
    report = run_connected(fixture=True)
    assert exit_status(report) == 1
    assert report["checks"][CHECKS[0]]["status"] == "BLOCKED"
    assert report["cleanup"]["status"] == "BLOCKED"


def test_unprovable_cleanup_is_never_silently_downgraded_to_not_run(monkeypatch):
    from cli.verification_process import CleanupError

    class UnprovableProcess:
        pid = 123

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("owned child", timeout)

        def finalize(self):
            raise CleanupError("injected native proof failure")

    monkeypatch.setattr("cli.verification_checks.source_identity", lambda: ("revision", "inputs"))
    monkeypatch.setattr("cli.verification_checks.OwnedProcess", lambda *args, **kwargs: UnprovableProcess())
    report = run_connected(fixture=True)
    assert report["cleanup"]["status"] == "BLOCKED"
    assert report["checks"][CHECKS[0]]["status"] == "FAIL"
    assert exit_status(report) == 1


def test_text_command_displays_unproven_cleanup(monkeypatch, capsys):
    from cli.verify_readiness import main

    report = new_report("invocation", "revision", "inputs")
    report["cleanup"] = {"status": "BLOCKED", "reason": "owned tree termination could not be proven"}
    monkeypatch.setattr("cli.verification_checks.run_connected", lambda **kwargs: report)
    assert main(["--connected", "--fixture"]) == 1
    assert "[BLOCKED] Isolated cleanup: owned tree termination could not be proven" in capsys.readouterr().out
