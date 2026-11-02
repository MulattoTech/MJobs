"""Operator command must execute isolated flows, never import an old PASS."""
import json
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[2]
requires_windows_connected = pytest.mark.skipif(
    sys.platform != "win32",
    reason="NOT_RUN: connected verifier requires Windows retained Job; see local Windows functional evidence",
)


def command(*args, env=None):
    return subprocess.run([sys.executable, "-m", "cli.verify_readiness", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=150)


@requires_windows_connected
def test_connected_command_executes_sources_and_durable_application():
    result = command("--connected", "--fixture", "--json", "--connected-timeout", "120")
    assert result.returncode in (0, 2), result.stdout + result.stderr
    assert result.stdout.strip(), "CLI must execute and report source/application checks"
    report = json.loads(result.stdout)
    assert report["checks"]["mock global source"]["status"] == "PASS"
    assert report["checks"]["mock company discovery"]["status"] == "PASS"
    assert report["checks"]["Document engine healthy"]["status"] == "PASS"
    if report["checks"]["Browser worker healthy"]["status"] == "NOT_RUN":
        assert result.returncode == 2
        assert report["checks"]["mock confirmation"]["status"] == "NOT_RUN"
    else:
        assert result.returncode == 0, report
        assert all(row["status"] == "PASS" for row in report["checks"].values())
        assert report["observations"]["submission_count"] == 1
        assert report["observations"]["persisted_state"] == "CONFIRMED"
        assert report["observations"]["dry_run_submission_count"] == 0
    assert report["invocation_id"]
    assert report["revision"]
    assert report["runtime"]["pid"] != 0
    assert not Path(report["workspace"]).exists(), "owned temporary data must be cleaned"
    assert report["cleanup"]["status"] == "PASS"


@requires_windows_connected
def test_explicit_dry_run_executes_complete_pipeline_with_zero_submissions():
    result = command("--dry-run", "--fixture", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["execution_mode"] == "DRY_RUN"
    assert all(row["status"] == "PASS" for row in report["checks"].values()), report
    assert report["observations"]["score"] == 100
    assert report["observations"]["scoring_operation_id"]
    assert report["observations"]["company_operation_id"]
    assert report["observations"]["submission_count"] == 0
    assert report["observations"]["persisted_state"] == "DRY_RUN_COMPLETE"
    assert "mock confirmation" not in report["checks"]
    assert report["cleanup"]["status"] == "PASS"
    assert not Path(report["workspace"]).exists()


@requires_windows_connected
def test_dry_run_never_enters_submit_path_and_parent_rejects_changed_artifacts(tmp_path):
    import sqlite3
    from cli.verification_checks import _corroborate, new_report, exit_status
    from cli.verification_fixture import exercise
    from services.applications import mock_adapter
    import workers.tasks

    report = new_report("dry-run-boundary", "revision", "inputs", mode="DRY_RUN")
    report["workspace"] = str(tmp_path)
    expected = deepcopy(report)
    original = mock_adapter.apply_mock
    calls = []

    def forbid_submission(*args, **kwargs):
        assert kwargs.get("submit") is False, "dry-run reached a submission boundary"
        calls.append(False)
        return original(*args, **kwargs)

    with patch.dict(os.environ), patch.object(mock_adapter, "apply_mock", forbid_submission), \
            patch.object(workers.tasks, "apply_mock", forbid_submission):
        exercise(report, tmp_path, 120)
    assert exit_status(report) == 0, report
    assert calls == [False]
    _corroborate(report, expected)
    database = tmp_path / "fixture.db"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE job_matches SET score = 50")
    with pytest.raises(ValueError, match="scoring explanation"):
        _corroborate(report, expected)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE job_matches SET score = 100")
        connection.execute("UPDATE applications SET state = 'CONFIRMED'")
    with pytest.raises(ValueError, match="dry-run persistence"):
        _corroborate(report, expected)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE applications SET state = 'DRY_RUN_COMPLETE'")
    counts_path = tmp_path / "request-counts.json"
    counts = json.loads(counts_path.read_text())
    counts["submit"] = 1
    counts_path.write_text(json.dumps(counts))
    with pytest.raises(ValueError, match="zero-submission"):
        _corroborate(report, expected)
    for submit, review in ((False, 1), (0.0, 1), (0, True), (0, 1.0)):
        counts.update(submit=submit, review=review)
        counts_path.write_text(json.dumps(counts))
        with pytest.raises(ValueError, match="zero-submission"):
            _corroborate(report, expected)


@requires_windows_connected
def test_no_op_rescore_cannot_pass_using_discovery_scores(tmp_path):
    from cli.verification_checks import new_report, exit_status
    from cli.verification_fixture import exercise
    import workers.tasks

    report = new_report("dry-run-no-op-score", "revision", "inputs", mode="DRY_RUN")
    # Discovery's own recalculation remains real; only the separately admitted
    # rescore worker branch is faulty and completes without writing new scores.
    with patch.dict(os.environ), patch.object(workers.tasks, "recalculate", return_value=None) as rescore:
        exercise(report, tmp_path, 120)
    rescore.assert_called_once()
    assert report["checks"]["mock global source"]["status"] == "PASS"
    assert report["checks"]["mock company discovery"]["status"] == "PASS"
    assert report["checks"]["Scoring functional"]["status"] == "FAIL"
    assert report["checks"]["Document engine healthy"]["status"] == "NOT_RUN"
    assert exit_status(report) == 1


@requires_windows_connected
def test_no_op_explicit_discovery_cannot_borrow_company_discovery_inventory(tmp_path):
    from cli.verification_checks import new_report, exit_status
    from cli.verification_fixture import exercise
    import workers.tasks

    report = new_report("dry-run-no-op-discovery", "revision", "inputs", mode="DRY_RUN")
    original = workers.tasks.discover
    calls = []

    def only_first_discovery(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs) if len(calls) == 1 else []

    with patch.dict(os.environ), patch.object(workers.tasks, "discover", side_effect=only_first_discovery):
        exercise(report, tmp_path, 120)
    assert len(calls) == 2
    assert report["checks"]["mock company discovery"]["status"] == "PASS"
    assert report["checks"]["mock global source"]["status"] == "FAIL"
    assert report["checks"]["Scoring functional"]["status"] == "NOT_RUN"
    assert exit_status(report) == 1


def test_connected_requires_explicit_fixture_admission():
    result = command("--connected", "--json")
    assert result.returncode != 0
    assert "--fixture" in result.stderr


@requires_windows_connected
def test_connected_repeats_with_new_identity_and_ignores_personal_environment(tmp_path):
    sentinel = tmp_path / "private.db"
    sentinel.write_bytes(b"private database must never be opened")
    env = dict(os.environ, MJOBS_DATABASE_URL="sqlite:///" + sentinel.as_posix(),
               MJOBS_OPERATION_MODE="PERSONAL", MJOBS_MOCK_ORIGIN="https://employer.invalid",
               MJOBS_EXTERNAL_SUBMISSION_ENABLED="1", HTTPS_PROXY="http://127.0.0.1:1")
    reports = []
    for _ in range(2):
        result = command("--connected", "--fixture", "--json", env=env)
        assert result.returncode in (0, 2), result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["checks"]["mock global source"]["status"] == "PASS"
        assert not Path(report["workspace"]).exists()
        reports.append(report)
    assert reports[0]["invocation_id"] != reports[1]["invocation_id"]
    assert reports[0]["observations"]["profile_id"] != reports[1]["observations"]["profile_id"]
    assert reports[0]["source_sha256"] == reports[1]["source_sha256"]
    assert sentinel.read_bytes() == b"private database must never be opened"


def test_connected_rejects_user_service_target():
    result = command("--connected", "--fixture", "--api", "https://employer.invalid", "--json")
    assert result.returncode != 0
    assert "owns its fixture target" in result.stderr


@requires_windows_connected
def test_deadline_cancels_owned_child_and_removes_temporary_files():
    result = command("--connected", "--fixture", "--json", "--connected-timeout", "1")
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["checks"]["adapter registry"]["status"] == "FAIL"
    assert "deadline" in report["checks"]["adapter registry"]["reason"]
    assert not any(row["status"] == "PASS" for row in report["checks"].values())
    assert not Path(report["workspace"]).exists()
    assert report["cleanup"]["status"] == "PASS"


@requires_windows_connected
def test_missing_browser_preserves_executed_sources_and_nonzero_partial(tmp_path):
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(tmp_path / "no-installed-browser"))
    result = command("--connected", "--fixture", "--json", env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["checks"]["adapter registry"]["status"] == "PASS"
    assert report["checks"]["mock global source"]["status"] == "PASS"
    assert report["checks"]["Document engine healthy"]["status"] == "PASS"
    assert report["checks"]["Browser worker healthy"]["status"] == "NOT_RUN"
    for name in ("mock confirmation", "mock upload", "duplicate-submission protection"):
        assert report["checks"][name]["status"] == "NOT_RUN"
    assert not Path(report["workspace"]).exists()


@requires_windows_connected
def test_executed_detector_failure_propagates_without_passing_downstream(tmp_path):
    from cli.verification_checks import new_report, exit_status
    from cli.verification_fixture import exercise
    from services.ats_detection.detector import detect_ats

    def fail_positive_control(url, html="", **kwargs):
        if "https://jobs.lever.co/fixture" in html:
            raise RuntimeError("synthetic detector failure")
        return detect_ats(url, html, **kwargs)

    report = new_report("failure-control", "revision", "source-hash")
    with patch.dict(os.environ), patch("services.ats_detection.detector.detect_ats",
                                      side_effect=fail_positive_control):
        exercise(report, tmp_path, 120)
    assert report["checks"]["mock global source"]["status"] == "PASS"
    assert report["checks"]["ATS detector"]["status"] == "FAIL"
    assert report["checks"]["mock confirmation"]["status"] == "NOT_RUN"
    assert exit_status(report) == 1


@requires_windows_connected
@pytest.mark.parametrize("stage", ["upload", "confirmation"])
def test_real_browser_failures_cannot_project_success(stage, tmp_path):
    import httpx
    from cli.verification_checks import new_report, exit_status
    from cli.verification_fixture import exercise
    from packages.persistence.store import Store
    from services.discovery.pipeline import MockClient
    from services.applications import upload_integrity

    report = new_report("failure-" + stage, "revision", "source-hash")
    original_get = MockClient.get

    def corrupt_confirmation(client, url):
        response = original_get(client, url)
        if "/ats/receipts/" in url:
            payload = response.json()
            if "receipt" in payload:
                payload["confirmation_text"] = "Your application is still being processed."
                return httpx.Response(response.status_code, json=payload, request=response.request)
        return response

    if stage == "upload":
        # Fail at the browser selected-File verification boundary. Everything
        # upstream, including document generation and Chromium launch, is real.
        fault = patch.object(upload_integrity, "verify_file_selection",
                             side_effect=upload_integrity.UploadIntegrityError("injected byte mismatch"))
    else:
        # The owned mock server and browser submit really execute, but corrupt
        # the read-back receipt text so it cannot certify confirmation.
        fault = patch.object(MockClient, "get", corrupt_confirmation)
    with patch.dict(os.environ), fault:
        exercise(report, tmp_path, 120)
    assert report["checks"]["Document engine healthy"]["status"] == "PASS"
    assert report["checks"]["Browser worker healthy"]["status"] == "PASS"
    assert exit_status(report) == 1
    store = Store(f"sqlite:///{(tmp_path / 'fixture.db').as_posix()}")
    try:
        apps = store.applications(report["observations"]["profile_id"])
        assert len(apps) == 1
        application = store.application(apps[0]["id"])
        assert application["confirmation"] is None
        receipts = json.loads((tmp_path / "receipts.json").read_text()) if (tmp_path / "receipts.json").exists() else {}
        if stage == "upload":
            assert report["checks"]["Dry-run mode functional"]["status"] == "FAIL"
            assert report["checks"]["mock upload"]["status"] == "NOT_RUN"
            assert report["checks"]["mock confirmation"]["status"] == "NOT_RUN"
            assert not receipts
        else:
            assert report["checks"]["mock upload"]["status"] == "PASS"
            assert report["checks"]["mock confirmation"]["status"] == "FAIL"
            assert report["checks"]["duplicate-submission protection"]["status"] == "NOT_RUN"
            assert application["state"] == "SUBMISSION_UNCONFIRMED"
            assert receipts[application["id"]]["submission_count"] == 1
    finally:
        store.engine.dispose()


@requires_windows_connected
@pytest.mark.parametrize("stage", ["duplicate", "readback"])
def test_parent_preserves_post_confirmation_failure_stage(stage, monkeypatch):
    from cli.verification_checks import run_connected, exit_status, validate_report
    from cli.verification_process import OwnedProcess

    # Execute the actual child workflow and parent protocol in an owned Job.
    # Only the selected late boundary fails; worker preparation/submit retain
    # their real implementation, imported before the replay-only fault.
    script = '''
import sys
from unittest.mock import patch
import workers.tasks
from packages.persistence.store import Store
from services.applications import mock_adapter
from cli.verification_fixture import main
stage=sys.argv.pop()
original_count=Store.count
def fail_readback(store, table):
 if table=='applications': raise RuntimeError('injected fresh-store readback failure')
 return original_count(store,table)
fault=(patch.object(mock_adapter,'apply_mock',side_effect=RuntimeError('injected duplicate replay failure'))
       if stage=='duplicate' else patch.object(Store,'count',fail_readback))
with fault: sys.exit(main())
'''

    def launch(argv, **kwargs):
        return OwnedProcess([sys.executable, "-c", script, *argv[3:], stage], **kwargs)

    def validate_with_tamper_controls(report, expected, *, pid):
        validate_report(report, expected, pid=pid)
        corrupted = deepcopy(report)
        corrupted["observations"]["submission_count"] = 2
        with pytest.raises(ValueError):
            validate_report(corrupted, expected, pid=pid)
        fabricated = deepcopy(report)
        fabricated["checks"]["durable readback"]["status"] = "PASS"
        with pytest.raises(ValueError):
            validate_report(fabricated, expected, pid=pid)

    monkeypatch.setattr("cli.verification_checks.OwnedProcess", launch)
    monkeypatch.setattr("cli.verification_checks.validate_report", validate_with_tamper_controls)
    report = run_connected(fixture=True, timeout=120)
    assert report["checks"]["adapter registry"]["status"] == "PASS", report
    assert report["checks"]["mock confirmation"]["status"] == "PASS"
    assert report["observations"]["submission_count"] == 1
    assert "persisted_state" not in report["observations"]
    assert report["checks"]["duplicate-submission protection"]["status"] == ("FAIL" if stage == "duplicate" else "PASS")
    assert report["checks"]["durable readback"]["status"] == ("NOT_RUN" if stage == "duplicate" else "FAIL")
    assert exit_status(report) == 1
    assert report["cleanup"]["status"] == "PASS"
    assert not Path(report["workspace"]).exists()


def test_default_readiness_performs_only_one_read_and_no_fixture_writes(tmp_path):
    requests = []

    class ReadinessServer(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(("GET", self.path))
            body = json.dumps({"status": "ready", "db_backend": "postgresql", "checks": {
                "configuration": True, "schema": True, "database": True, "queue": True,
                "scheduler": True, "worker": True, "workers": {
                    "mjobs-application": True, "mjobs-company-discovery": True,
                    "mjobs-discovery": True, "mjobs-document": True,
                }}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ReadinessServer)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sentinel = tmp_path / "private.db"
    sentinel.write_bytes(b"untouched private data")
    env = dict(os.environ, MJOBS_DATABASE_URL="sqlite:///" + sentinel.as_posix(),
               TEMP=str(tmp_path), TMP=str(tmp_path))
    try:
        result = command("--api", "http://127.0.0.1:" + str(server.server_port), env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "[PASS] PostgreSQL reachable" in result.stdout
        assert "mock confirmation" not in result.stdout
        assert requests == [("GET", "/readiness")]
        assert list(tmp_path.iterdir()) == [sentinel]
        assert sentinel.read_bytes() == b"untouched private data"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="NOT_RUN: non-Windows contract is exercised by Linux CI")
def test_non_windows_connected_command_is_blocked_without_functional_pass():
    result = command("--connected", "--fixture", "--json")
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["checks"]["adapter registry"]["status"] == "BLOCKED"
    assert report["cleanup"]["status"] == "BLOCKED"
    assert not any(row["status"] == "PASS" for row in report["checks"].values())
