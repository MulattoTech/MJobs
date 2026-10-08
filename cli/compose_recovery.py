"""Controlled outage/restart proof for this project's fictional Compose stack."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
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
    containers = compose(project_name, port, "ps", "-q", "worker").split()
    if len(containers) != 1:
        raise RuntimeError("Project recovery worker target was missing or ambiguous")
    inspected = _run_bounded(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}|{{.RestartCount}}", containers[0]],
        timeout=10, action="Inspect recovery worker",
    ).strip()
    labels_json, separator, restart_count = inspected.partition("|")
    if not separator:
        raise RuntimeError("Project recovery worker inspection was malformed")
    try:
        labels = json.loads(labels_json)
        count = int(restart_count)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Project recovery worker inspection was malformed") from exc
    if (not isinstance(labels, dict)
            or labels.get("com.docker.compose.project") != project_name
            or labels.get("com.docker.compose.service") != "worker"):
        raise RuntimeError("Project recovery worker inspection returned an unexpected target")
    return count


def run(baseline_path, evidence_path, project_name, port):
    project_name = validate_project_name(project_name)
    port = validate_port(port)
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    for key in ("application_id", "operation_id", "confirmation"):
        if key not in baseline:
            raise ValueError("Baseline is missing required recovery evidence: " + key)
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
            application = request("GET", "/profiles/" + baseline["profile_id"] + "/applications/" + baseline["application_id"])
            assert application["state"] == "CONFIRMED"
            assert application["confirmation"] == baseline["confirmation"]
            operation = request("GET", "/operations/" + baseline["operation_id"])
            assert operation["state"] == "COMPLETED"
            again = request("POST", "/profiles/" + baseline["profile_id"] + "/applications/" + baseline["application_id"] + "/mock-run", json={"submit": True})
            assert again["operation_id"] == baseline["operation_id"] and again["state"] == "COMPLETED"
            # Verify the restarted mock server's independently persisted receipt.
            program = "import json,sys,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://mock:8090/ats/receipts/'+sys.argv[1],timeout=5))))"
            receipt = json.loads(compose(project_name, port, "exec", "-T", "api", "python", "-c", program, baseline["application_id"]))
            assert receipt == baseline["confirmation"] and receipt["submission_count"] == 1
            actions.append({"action": "All services restarted; original application, operation and independent receipt survived without another submission", "result": "PASS"})
        finally:
            compose(project_name, port, "start", "redis", "worker", "source-worker", "document-worker")
    evidence = {"result": "PASS", "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": ("python -m cli.compose_recovery --baseline " + str(baseline_path)
                    + " --evidence " + str(evidence_path)
                    + " --project-name " + project_name + " --port " + str(port)),
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
