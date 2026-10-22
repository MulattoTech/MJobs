"""Run owned synthetic verification. No target URL or evidence-file import API.

Exit 0: all connected synthetic checks executed successfully; 1: failure or
blocked admission; 2: dependencies unavailable. These are not deployment health
or whole-product acceptance claims. Runtime readiness remains a separate mode.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from uuid import uuid4

from cli.verification_process import CleanupError, ContainmentUnavailable, OwnedProcess


ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    "adapter registry", "mock global source", "mock company discovery", "ATS detector",
    "Document engine healthy", "Browser worker healthy", "Dry-run mode functional",
    "mock application fill", "mock upload", "mock confirmation",
    "duplicate-submission protection", "durable readback",
)


def validate_connected_timeout(value):
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= 120:
        raise ValueError("connected timeout must be from 1 through 120 seconds")
    return value


def source_identity():
    """Identify actual inputs, including local edits, not merely the Git HEAD."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=5).strip()
    digest = hashlib.sha256()
    for folder in ("cli", "apps/api", "packages", "services", "workers", "fixtures"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    for name in ("requirements.lock", "tests/integration/test_spine.py"):
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    return revision, digest.hexdigest()


def new_report(invocation_id, revision, source_hash):
    return {
        "schema_version": 1, "scope": "isolated synthetic FIXTURE; not deployment readiness",
        "invocation_id": invocation_id, "revision": revision, "source_sha256": source_hash,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "external_submission_enabled": False,
        "checks": {name: {"status": "NOT_RUN", "reason": "prerequisite not completed"}
                   for name in CHECKS}, "observations": {},
    }


def validate_report(report, expected, *, pid):
    """Only accept the just-launched child's report for the exact invocation."""
    if not isinstance(report, dict):
        raise ValueError("invalid connected evidence")
    for key in ("schema_version", "invocation_id", "revision", "source_sha256", "started_at"):
        if report.get(key) != expected[key]:
            raise ValueError("connected evidence identity mismatch")
    runtime = report.get("runtime")
    # Windows venv python.exe is a launcher: the executing interpreter can be
    # its direct child. Both identities are observed by the interpreter itself.
    if (not isinstance(runtime, dict) or pid not in (runtime.get("pid"), runtime.get("parent_pid"))
            or runtime.get("python") != sys.version.split()[0]
            or report.get("external_submission_enabled") is not False):
        raise ValueError("connected evidence runtime mismatch")
    checks = report.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(CHECKS):
        raise ValueError("invalid connected check set")
    for row in checks.values():
        if (not isinstance(row, dict) or row.get("status") not in {"PASS", "FAIL", "NOT_RUN", "BLOCKED"}
                or not isinstance(row.get("reason"), str) or not row["reason"]):
            raise ValueError("invalid connected check result")
    finished = datetime.fromisoformat(report["finished_at"])
    started = datetime.fromisoformat(expected["started_at"])
    if not started <= finished <= datetime.now(timezone.utc):
        raise ValueError("stale connected evidence")
    if any(row["status"] == "PASS" for row in checks.values()):
        _corroborate(report, expected)


def _corroborate(report, expected):
    """Read back owned on-disk artifacts before accepting successful flags.

    Paths come from the parent's fresh temporary directory, never evidence.
    This also rejects an all-PASS JSON object without executed side effects.
    """
    observed = report.get("observations")
    if not isinstance(observed, dict) or report.get("workspace") != expected.get("workspace"):
        raise ValueError("connected observations missing or workspace mismatch")
    checks = report["checks"]
    passed = lambda name: checks[name]["status"] == "PASS"
    # A PASS from the global source probe depends on the registry's selected
    # source identity too; do not let downstream success bypass that evidence
    # when the registry row is FAIL or NOT_RUN.
    if passed("adapter registry") or passed("mock global source"):
        if observed.get("registry_requests") != [
                "/v1/boards/fixture", "/v1/boards/fixture/jobs"]:
            raise ValueError("registry request evidence missing")
        expected_source = {
            "registry_company": "Synthetic employer",
            "registry_job_requisition": "288",
            "registry_provider": "greenhouse",
            "registry_tenant": "fixture",
        }
        if any(observed.get(name) != value for name, value in expected_source.items()):
            raise ValueError("registry source identity corroboration missing")
    if not passed("mock global source"):
        return
    profile_id = "verification-" + expected["invocation_id"]
    if observed.get("profile_id") != profile_id or observed.get("canonical_jobs") != 2:
        raise ValueError("discovery identity mismatch")
    root = Path(expected["workspace"])
    with closing(sqlite3.connect((root / "fixture.db").as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("SELECT COUNT(*) FROM job_postings").fetchone()[0] != 4:
            raise ValueError("discovery persistence missing")
        if passed("mock company discovery") and db.execute(
                "SELECT COUNT(*) FROM company_job_sources").fetchone()[0] != 1:
            raise ValueError("company persistence missing")
        if passed("Document engine healthy"):
            rows = db.execute("SELECT application_id, files, sha256 FROM documents").fetchall()
            if len(rows) != 2:
                raise ValueError("document persistence missing")
            for app_id, files_json, hashes_json in rows:
                files, hashes = json.loads(files_json), json.loads(hashes_json)
                for extension in ("pdf", "docx"):
                    path = (root / app_id / files[extension]).resolve()
                    if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != hashes[extension]:
                        raise ValueError("document evidence mismatch")
        if passed("mock confirmation") or passed("duplicate-submission protection") or passed("durable readback"):
            row = db.execute("SELECT id, state, confirmation FROM applications WHERE profile_id = ?", (profile_id,)).fetchall()
            if len(row) != 1 or row[0][1] != "CONFIRMED":
                raise ValueError("confirmed application persistence missing")
            receipt = json.loads((root / "receipts.json").read_text(encoding="utf-8"))[row[0][0]]
            confirmation = json.loads(row[0][2])
            if (receipt["submission_count"] != 1 or receipt["receipt"] != confirmation["receipt"]
                    or observed.get("submission_count") != 1):
                raise ValueError("confirmation evidence mismatch")
            if passed("durable readback") and observed.get("persisted_state") != "CONFIRMED":
                raise ValueError("durable readback evidence mismatch")


def exit_status(report):
    statuses = {row["status"] for row in report["checks"].values()}
    if report.get("cleanup", {}).get("status") in {"FAIL", "BLOCKED"}:
        return 1
    if statuses & {"FAIL", "BLOCKED"}:
        return 1
    return 2 if "NOT_RUN" in statuses else 0


def _stop(process):
    """Finalize retained Job ownership, independently of direct-root liveness."""
    process.finalize()


def run_connected(*, fixture=False, timeout=120):
    timeout = validate_connected_timeout(timeout)
    revision, source_hash = source_identity()
    report = new_report(uuid4().hex, revision, source_hash)
    if fixture is not True:
        report["checks"][CHECKS[0]] = {"status": "BLOCKED", "reason": "explicit --fixture admission required"}
        return report
    try:
        return _execute_connected(report, timeout)
    except ContainmentUnavailable:
        report["checks"][CHECKS[0]] = {"status": "BLOCKED", "reason": "atomic retained process-tree containment unavailable; no uncontained fallback"}
        report["cleanup"] = {"status": "BLOCKED", "reason": "contained execution was not admitted"}
        return report
    except CleanupError:
        report["cleanup"] = {"status": "BLOCKED", "reason": "owned process-tree cleanup could not be proven"}
        return report
    except (OSError, subprocess.SubprocessError):
        # Permission/missing local executable errors are dependencies, not a
        # successful check or a dump of arbitrary private environment details.
        for row in report["checks"].values():
            row.update(status="NOT_RUN", reason="local execution or temporary-storage dependency unavailable")
        return report


def _execute_connected(report, timeout):
    revision, source_hash = report["revision"], report["source_sha256"]
    # Do not inherit personal database, service, provider, proxy or credential settings.
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
               "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME", "PLAYWRIGHT_BROWSERS_PATH"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["PYTHONUTF8"] = "1"
    temporary = tempfile.TemporaryDirectory(prefix="mjobs-connected-verification-")
    process = None
    cleanup_failed = False
    try:
        root = Path(temporary.name)
        request = root / "invocation.json"
        output = root / "result.json"
        report["workspace"] = str(root / "fixture")
        request.write_text(json.dumps(report), encoding="utf-8")
        process = OwnedProcess(
            [sys.executable, "-m", "cli.verification_fixture", str(request), str(output), str(timeout)],
            cwd=ROOT, env=env,
        )
        try:
            process.wait(timeout=timeout)
            received = json.loads(output.read_text(encoding="utf-8"))
            validate_report(received, report, pid=process.pid)
            if source_identity() != (revision, source_hash):
                raise ValueError("verification inputs changed during invocation")
            if process.returncode != exit_status(received):
                raise ValueError("connected child exit/evidence disagreement")
            report = received
        except subprocess.TimeoutExpired:
            report["checks"][CHECKS[0]] = {"status": "FAIL", "reason": "connected execution deadline exceeded; child cancelled"}
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            report["checks"][CHECKS[0]] = {"status": "FAIL", "reason": "missing, malformed or mismatched current child evidence"}
    finally:
        # Every path, including a valid report or crashed/exited host, finalizes
        # the Job before deleting inputs. A dead numeric PID is never used.
        if process is not None:
            try:
                _stop(process)
            except CleanupError:
                cleanup_failed = True
        try:
            temporary.cleanup()
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            report["cleanup"] = {"status": "BLOCKED", "reason": "owned tree termination or workspace removal could not be proven"}
        elif process is None:
            report["cleanup"] = {"status": "NOT_RUN", "reason": "no contained child admitted; temporary workspace removed"}
        else:
            report["cleanup"] = {"status": "PASS", "reason": "retained Job has zero active members; original host reaped and owned workspace removed"}
    return report
