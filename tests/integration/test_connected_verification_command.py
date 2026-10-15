"""Operator command must execute isolated flows, never import an old PASS."""
import json
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
