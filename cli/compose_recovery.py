"""Controlled outage/restart proof for this project's fictional Compose stack."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx

from tests.integration.test_spine import PROFILE


MAX_CAPTURED_OUTPUT_BYTES = 2 * 1024 * 1024
_OUTPUT_CHUNK_BYTES = 64 * 1024
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yml"
PROJECT_NAME_PATTERN = re.compile(r"mjobs-fixture-[a-f0-9]{12}\Z")
RECOVERY_RESTART_SERVICES = (
    "postgres", "redis", "mock", "api", "scheduler", "worker", "source-worker", "document-worker",
)
RECOVERY_MUTATION_SERVICES = RECOVERY_RESTART_SERVICES
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _run_bounded(command, *, timeout, action, environment=None):
    """Run a local recovery command with bounded retained stdout and a hard deadline."""
    try:
        process = subprocess.Popen(
            list(command), env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        )
    except OSError as exc:
        raise RuntimeError(f"{action} could not start") from exc

    captured = bytearray()
    output_exceeded = threading.Event()

    def drain_stdout():
        try:
            while True:
                chunk = process.stdout.read(_OUTPUT_CHUNK_BYTES)
                if not chunk:
                    return
                available = max(0, MAX_CAPTURED_OUTPUT_BYTES - len(captured))
                if available:
                    captured.extend(chunk[:available])
                if len(chunk) > available:
                    output_exceeded.set()
        except (OSError, ValueError):
            return

    def drain_stderr():
        try:
            while process.stderr.read(_OUTPUT_CHUNK_BYTES):
                pass
        except (OSError, ValueError):
            return

    stdout_reader = threading.Thread(target=drain_stdout, daemon=True)
    stderr_reader = threading.Thread(target=drain_stderr, daemon=True)
    stdout_reader.start()
    stderr_reader.start()

    def stop_child():
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=5)

    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if output_exceeded.is_set():
                stop_child()
                raise RuntimeError(f"{action} exceeded the captured-output limit")
            if time.monotonic() >= deadline:
                stop_child()
                raise RuntimeError(f"{action} timed out")
            time.sleep(0.01)

        returncode = process.wait()
        stdout_reader.join(timeout=1)
        stderr_reader.join(timeout=1)
        if output_exceeded.is_set():
            raise RuntimeError(f"{action} exceeded the captured-output limit")
        if returncode:
            raise RuntimeError(f"{action} failed with exit code {returncode}")
        text = bytes(captured).decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n").replace("\r", "\n")
    finally:
        if process.poll() is None:
            stop_child()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        stdout_reader.join(timeout=1)
        stderr_reader.join(timeout=1)


def validate_project_name(value):
    if not isinstance(value, str) or PROJECT_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError("Project name must match mjobs-fixture-[a-f0-9]{12}")
    return value


def validate_port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise ValueError("Port must be an integer from 1024 through 65535")
    return value


def compose(project_name, port, *arguments):
    project_name = validate_project_name(project_name)
    port = validate_port(port)
    environment = os.environ.copy()
    environment.update(
        MJOBS_OPERATION_MODE="FIXTURE",
        MJOBS_COMPOSE_MOCK="1",
        COMPOSE_PROFILES="fixture",
        COMPOSE_PROJECT_NAME=project_name,
        MJOBS_PORT=str(port),
    )
    command = [
        "docker", "compose", "--file", str(COMPOSE_FILE),
        "--project-directory", str(REPOSITORY_ROOT),
        "--profile", "fixture", "--project-name", project_name,
        *arguments,
    ]
    return _run_bounded(command,
                        environment=environment, timeout=90, action="Project Compose action")


def restart_recovery_services(project_name, port):
    """Restart every stateful service, including the durable discovery scheduler."""
    return compose(project_name, port, "restart", *RECOVERY_RESTART_SERVICES)


def worker_restart_count(project_name, port):
    """Read the restart count for this project's worker only."""
    containers = compose(project_name, port, "ps", "--all", "-q", "worker").split()
    if len(containers) != 1:
        raise RuntimeError("Project recovery worker target was missing or ambiguous")
    return inspect_container(project_name, "worker", containers[0])["restart_count"]


def inspect_container(project_name, service, container_id):
    inspected = _run_bounded(
        ["docker", "inspect", "--format",
         "{{json .Config.Labels}}|{{.RestartCount}}|{{.State.Status}}", container_id],
        timeout=10, action="Inspect recovery container",
    ).strip()
    pieces = inspected.split("|")
    if len(pieces) != 3:
        raise RuntimeError("Project recovery container inspection was malformed")
    try:
        labels = json.loads(pieces[0])
        count = int(pieces[1])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Project recovery container inspection was malformed") from exc
    expected_labels = {
        "com.docker.compose.project": project_name,
        "com.docker.compose.service": service,
        "com.docker.compose.project.config_files": str(COMPOSE_FILE.resolve()),
        "com.docker.compose.project.working_dir": str(REPOSITORY_ROOT.resolve()),
    }
    if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected_labels.items()):
        raise RuntimeError(f"Project recovery {service} inspection returned an unexpected target")
    if pieces[2] != "running":
        raise RuntimeError(f"Project recovery {service} target is not running")
    return {"restart_count": count, "status": pieces[2]}


def preflight_recovery_targets(project_name, port):
    """Verify every container target before any recovery stop/start/restart command."""
    targets = {}
    for service in RECOVERY_MUTATION_SERVICES:
        containers = compose(project_name, port, "ps", "--all", "-q", service).split()
        if len(containers) != 1:
            raise RuntimeError(f"Project recovery {service} target was missing or ambiguous")
        inspected = inspect_container(project_name, service, containers[0])
        targets[service] = {"container_id": containers[0], **inspected}
    return targets


def validate_baseline(baseline):
    """Require the exact connected UI evidence needed for receipt-only replay."""
    if not isinstance(baseline, dict):
        raise ValueError("Baseline must be a UI evidence object")
    for key in ("profile_id", "application_id", "operation_id", "request_id"):
        value = baseline.get(key)
        if not isinstance(value, str) or REQUEST_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("Baseline is missing or has an invalid recovery identifier: " + key)
    if baseline.get("external_submission_enabled") is not False:
        raise ValueError("Baseline must prove external submission is disabled")
    confirmation = baseline.get("confirmation")
    if (not isinstance(confirmation, dict)
            or confirmation.get("application_id") != baseline["application_id"]
            or not isinstance(confirmation.get("receipt"), str)
            or not confirmation["receipt"]
            or type(confirmation.get("submission_count")) is not int
            or confirmation["submission_count"] != 1):
        raise ValueError("Baseline must contain the matching single mock confirmation")
    return baseline


def verify_recovered_application(request, baseline):
    """Read current durable state and request identity; never queue an unknown intent."""
    prefix = ("/profiles/" + baseline["profile_id"] + "/applications/"
              + baseline["application_id"])
    application = request("GET", prefix)
    if (application.get("state") != "CONFIRMED"
            or application.get("confirmation") != baseline["confirmation"]):
        raise RuntimeError("Baseline application is not the same confirmed mock receipt")
    operation = request("GET", "/operations/" + baseline["operation_id"])
    if operation.get("state") != "COMPLETED":
        raise RuntimeError("Baseline application operation is not completed")
    recovered = request("GET", prefix + "/mock-runs/" + baseline["request_id"])
    if (recovered.get("state") != "COMPLETED"
            or recovered.get("operation_id") != baseline["operation_id"]
            or recovered.get("external_submission_enabled") is not False):
        raise RuntimeError("Baseline explicit mock run was not recovered as completed")
    return prefix


def run(baseline_path, evidence_path, project_name, port):
    project_name = validate_project_name(project_name)
    port = validate_port(port)
    baseline = validate_baseline(json.loads(Path(baseline_path).read_text(encoding="utf-8")))
    initial_targets = preflight_recovery_targets(project_name, port)
    api = f"http://127.0.0.1:{port}"
    actions = []
    with httpx.Client(base_url=api, timeout=15, trust_env=False) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        def until(predicate):
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    result = predicate()
                    if result:
                        return result
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.5)
            raise TimeoutError("Project recovery did not complete within 120 seconds")

        # Validate supplied UI evidence and durable caller identity before stopping anything.
        application_path = verify_recovered_application(request, baseline)
        services_to_restore = tuple(
            service for service in RECOVERY_RESTART_SERVICES
            if initial_targets[service]["status"] == "running"
        )
        try:
            compose(project_name, port, "stop", "worker", "source-worker", "document-worker", "redis")
            profile = dict(PROFILE, id="recovery-fictional-" + uuid4().hex[:12])
            request("PUT", "/profiles", json=profile)
            queued = request("POST", "/discovery", json={"profile_id": profile["id"]})
            assert queued["state"] == "QUEUED"
            degraded = client.get("/readiness")
            assert degraded.status_code == 503 and degraded.json()["checks"]["queue"] is False
            actions.append({"action": "Redis disconnected; discovery persisted", "result": "PASS", "operation_id": queued["operation_id"]})
            # --no-deps prevents Compose from helpfully restoring Redis early.
            compose(project_name, port, "up", "-d", "--no-deps", "--no-build", "worker")
            until(lambda: worker_restart_count(project_name, port) > 0)
            compose(project_name, port, "start", "redis")
            compose(project_name, port, "start", "worker", "source-worker", "document-worker")
            until(lambda: request("GET", "/operations/" + queued["operation_id"])["state"] == "COMPLETED")
            until(lambda: client.get("/readiness").status_code == 200)
            actions.append({"action": "Worker supervision and durable outbox recovered after Redis returned", "result": "PASS"})
            restart_recovery_services(project_name, port)
            until(lambda: client.get("/readiness").status_code == 200)
            application_path = verify_recovered_application(request, baseline)
            again = request("POST", application_path + "/mock-run", json={
                "submit": True, "request_id": baseline["request_id"],
            })
            if (again.get("operation_id") != baseline["operation_id"]
                    or again.get("state") != "COMPLETED"
                    or again.get("replayed") is not True
                    or again.get("external_submission_enabled") is not False):
                raise RuntimeError("Explicit mock run replay returned an unexpected operation")
            # Verify the restarted mock server's independently persisted receipt.
            program = "import json,sys,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://mock:8090/ats/receipts/'+sys.argv[1],timeout=5))))"
            receipt = json.loads(compose(project_name, port, "exec", "-T", "api", "python", "-c", program, baseline["application_id"]))
            assert receipt == baseline["confirmation"] and receipt["submission_count"] == 1
            actions.append({"action": "All services restarted; original application, operation and independent receipt survived without another submission", "result": "PASS"})
        finally:
            compose(project_name, port, "start", *services_to_restore)
    evidence = {"result": "PASS", "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": [sys.executable, "-m", "cli.compose_recovery", "--baseline", str(baseline_path),
                    "--evidence", str(evidence_path), "--project-name", project_name,
                    "--port", str(port)],
        "compose_target": {"project_name": project_name, "port": port,
                           "compose_file": str(COMPOSE_FILE.resolve()),
                           "project_directory": str(REPOSITORY_ROOT.resolve()),
                           "profile": "fixture", "services": list(RECOVERY_RESTART_SERVICES)},
        "target": "Project-only PostgreSQL/Redis/RQ/mock persistence and outage recovery", "actions": actions,
        "application_id": baseline["application_id"], "external_submission_enabled": False}
    Path(evidence_path).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--project-name", required=True, type=validate_project_name)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        port = validate_port(args.port)
    except ValueError as exc:
        parser.error(str(exc))
    run(args.baseline, args.evidence, args.project_name, port)


if __name__ == "__main__":
    main()
