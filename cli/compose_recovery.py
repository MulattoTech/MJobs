"""Controlled outage/restart proof for this project's fictional Compose stack."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4
from urllib.parse import quote

import httpx

from tests.integration.test_spine import PROFILE


MAX_CAPTURED_OUTPUT_BYTES = 2 * 1024 * 1024
_OUTPUT_CHUNK_BYTES = 64 * 1024
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yml"
PROJECT_NAME_PATTERN = re.compile(r"mjobs-fixture-[a-f0-9]{12}\Z")
RECOVERY_RESTART_SERVICES = (
    "postgres", "redis", "mock", "api", "scheduler", "worker", "source-worker",
    "company-discovery-worker", "document-worker", "ai-worker",
)
RECOVERY_MUTATION_SERVICES = RECOVERY_RESTART_SERVICES
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
JOB_INVENTORY_VERSION = 1
CANONICAL_JOB_FIELDS = (
    "company_id", "requisition", "title", "description", "skills", "location",
    "remote_type", "attributes", "company",
)
JOB_ROUTE_FIELDS = ("id", "company_id", "provider", "tenant", "requisition", "url")
SOURCE_COPY_FIELDS = ("source", "source_job_id", "source_url", "application_url")


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


def _path_contains_symlink(path):
    candidate = Path(path)
    absolute = candidate if candidate.is_absolute() else REPOSITORY_ROOT / candidate
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if part == "..":
            current = current.parent
            continue
        if part == ".":
            continue
        current = current / part
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (is_junction is not None and is_junction()):
            return True
    return False


def resolve_compose_files(value=None):
    """Resolve an explicit root-first Compose file list confined to this checkout."""
    root = REPOSITORY_ROOT.resolve(strict=True)
    if _path_contains_symlink(COMPOSE_FILE):
        raise ValueError("Compose files must not be symlinks")
    root_compose = COMPOSE_FILE.resolve(strict=True)
    if value is None:
        value = os.environ.get("COMPOSE_FILE")
    raw_files = [str(root_compose)] if value is None or value == "" else value.split(os.pathsep)
    if not raw_files or any(not raw_path for raw_path in raw_files):
        raise ValueError("COMPOSE_FILE must contain non-empty file paths")

    resolved_files = []
    seen = set()
    for raw_path in raw_files:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if _path_contains_symlink(candidate):
            raise ValueError("Compose files must not be symlinks")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Compose file does not exist") from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("Compose files must resolve inside the canonical workspace") from exc
        if not resolved.is_file():
            raise ValueError("Compose files must be regular files")
        if "," in str(resolved):
            raise ValueError("Compose file paths cannot contain commas")
        identity = os.path.normcase(str(resolved))
        if identity in seen:
            raise ValueError("COMPOSE_FILE must not contain duplicate paths")
        seen.add(identity)
        resolved_files.append(resolved)

    if not resolved_files or os.path.normcase(str(resolved_files[0])) != os.path.normcase(str(root_compose)):
        raise ValueError("COMPOSE_FILE must start with the repository compose.yml")
    return tuple(resolved_files)


def expected_config_files_label(config_files=None):
    config_files = _validated_compose_files(config_files)
    return ",".join(str(path) for path in config_files)


def _validated_compose_files(config_files=None):
    if config_files is None:
        return resolve_compose_files()
    return resolve_compose_files(os.pathsep.join(str(path) for path in config_files))


def compose(project_name, port, *arguments, config_files=None):
    project_name = validate_project_name(project_name)
    port = validate_port(port)
    config_files = _validated_compose_files(config_files)
    environment = os.environ.copy()
    environment.update(
        MJOBS_OPERATION_MODE="FIXTURE",
        MJOBS_COMPOSE_MOCK="1",
        COMPOSE_PROFILES="fixture",
        COMPOSE_PROJECT_NAME=project_name,
        MJOBS_PORT=str(port),
        COMPOSE_FILE=os.pathsep.join(str(path) for path in config_files),
    )
    command = [
        "docker", "compose",
        *[item for path in config_files for item in ("--file", str(path))],
        "--project-directory", str(REPOSITORY_ROOT.resolve()),
        "--profile", "fixture", "--project-name", project_name,
        *arguments,
    ]
    return _run_bounded(command,
                        environment=environment, timeout=90, action="Project Compose action")


def restart_recovery_services(project_name, port, *, config_files=None):
    """Restart every stateful service, including the durable discovery scheduler."""
    return compose(project_name, port, "restart", *RECOVERY_RESTART_SERVICES, config_files=config_files)


def worker_restart_count(project_name, port, *, config_files=None):
    """Read the restart count for this project's worker only."""
    containers = compose(project_name, port, "ps", "--all", "-q", "worker", config_files=config_files).split()
    if len(containers) != 1:
        raise RuntimeError("Project recovery worker target was missing or ambiguous")
    # Redis is deliberately unavailable here: the supervised worker can be
    # between exits and restarts. Observe its count without relaxing preflight.
    return inspect_container(project_name, "worker", containers[0], config_files=config_files,
                             require_running=False)["restart_count"]


def inspect_container(project_name, service, container_id, *, config_files=None, require_running=True):
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
        "com.docker.compose.project.config_files": expected_config_files_label(config_files),
        "com.docker.compose.project.working_dir": str(REPOSITORY_ROOT.resolve()),
    }
    if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected_labels.items()):
        raise RuntimeError(f"Project recovery {service} inspection returned an unexpected target")
    allowed_statuses = ("running",) if require_running else ("running", "restarting", "exited")
    if pieces[2] not in allowed_statuses:
        raise RuntimeError(f"Project recovery {service} target is not running")
    return {"restart_count": count, "status": pieces[2]}


def preflight_recovery_targets(project_name, port, *, config_files=None):
    """Verify every container target before any recovery stop/start/restart command."""
    targets = {}
    for service in RECOVERY_MUTATION_SERVICES:
        containers = compose(project_name, port, "ps", "--all", "-q", service, config_files=config_files).split()
        if len(containers) != 1:
            raise RuntimeError(f"Project recovery {service} target was missing or ambiguous")
        inspected = inspect_container(project_name, service, containers[0], config_files=config_files)
        targets[service] = {"container_id": containers[0], **inspected}
    return targets


def container_health(container_id):
    """Read bounded status-only health evidence for a preflight-verified container."""
    result = _run_bounded([
        "docker", "inspect", "--format",
        "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",
        container_id,
    ], timeout=10, action="Inspect fixture container health").strip().split("|")
    if (len(result) != 2 or result[0] not in {
            "created", "running", "paused", "restarting", "removing", "exited", "dead"}
            or result[1] not in {"healthy", "unhealthy", "starting"}):
        raise RuntimeError("Fixture container health check is missing or malformed")
    return {"status": result[0], "health": result[1]}


def healthy_containers(targets):
    if set(targets) != set(RECOVERY_RESTART_SERVICES):
        raise RuntimeError("Fixture container health targets are incomplete")
    snapshot = {service: container_health(target["container_id"])
                for service, target in targets.items()}
    return snapshot if all(row == {"status": "running", "health": "healthy"}
                           for row in snapshot.values()) else False


def validate_implementation_progress(progress):
    """Validate that implementation progress contains required authoritative checkpoint fields."""
    if isinstance(progress, (str, Path)):
        progress_path = Path(progress)
        if not progress_path.is_file():
            raise ValueError(f"Implementation progress file does not exist: {progress}")
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Implementation progress file is corrupt: {progress}") from exc
    if not isinstance(progress, dict):
        raise ValueError("Implementation progress must be a dictionary")
    from tools.verification.progress import AUTHORITATIVE_FIELDS
    for field in AUTHORITATIVE_FIELDS:
        if field not in progress:
            raise ValueError(f"Implementation progress missing authoritative field: {field}")
        if field in ("completed_steps", "files_added", "files_modified", "tests_completed",
                     "tests_remaining", "migrations_applied", "live_integrations_tested",
                     "known_errors", "known_limitations"):
            if not isinstance(progress[field], (list, tuple)):
                raise ValueError(f"Implementation progress field {field} must be a list")
        elif field in ("current_phase", "last_safe_checkpoint"):
            if not isinstance(progress[field], (str, dict)) or (isinstance(progress[field], str) and not progress[field].strip()):
                raise ValueError(f"Implementation progress field {field} must be a valid checkpoint value")
    return progress


def validate_baseline(baseline):
    """Require the exact connected UI evidence needed for receipt-only replay."""
    if not isinstance(baseline, dict):
        raise ValueError("Baseline must be a UI evidence object")
    if 'local_persistence' in baseline:
        from cli.local_persistence_acceptance import validate
        validate(baseline['local_persistence'])
    if 'implementation_progress' in baseline:
        validate_implementation_progress(baseline['implementation_progress'])
    for key in ("profile_id", "application_id", "operation_id", "request_id"):
        value = baseline.get(key)
        if not isinstance(value, str) or REQUEST_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("Baseline is missing or has an invalid recovery identifier: " + key)
    if baseline.get("external_submission_enabled") is not False:
        raise ValueError("Baseline must prove external submission is disabled")
    if "job_inventory" in baseline:
        validate_public_job_inventory(baseline["job_inventory"])
        if baseline["job_inventory"]["profile_id"] != baseline["profile_id"]:
            raise ValueError("Baseline job inventory must match its profile")
    confirmation = baseline.get("confirmation")
    if (not isinstance(confirmation, dict)
            or confirmation.get("application_id") != baseline["application_id"]
            or not isinstance(confirmation.get("receipt"), str)
            or not confirmation["receipt"]
            or type(confirmation.get("submission_count")) is not int
            or confirmation["submission_count"] != 1):
        raise ValueError("Baseline must contain the matching single mock confirmation")
    return baseline


def _inventory_digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def validate_public_job_inventory(inventory):
    if (not isinstance(inventory, dict)
            or set(inventory) != {"schema_version", "profile_id", "job_count",
                                  "source_copy_count", "jobs", "sha256"}
            or type(inventory["schema_version"]) is not int
            or inventory["schema_version"] != JOB_INVENTORY_VERSION
            or not isinstance(inventory["profile_id"], str) or not inventory["profile_id"]
            or type(inventory["job_count"]) is not int or inventory["job_count"] < 1
            or type(inventory["source_copy_count"]) is not int or inventory["source_copy_count"] < 1
            or not isinstance(inventory["jobs"], list)
            or len(inventory["jobs"]) != inventory["job_count"]
            or not isinstance(inventory["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", inventory["sha256"]) is None):
        raise ValueError("Invalid public job/source-copy inventory")
    ids = set()
    total_sources = 0
    for row in inventory["jobs"]:
        if (not isinstance(row, dict)
                or set(row) != {"job_id", "canonical_sha256", "source_copy_count",
                                "source_copies_sha256"}
                or not isinstance(row["job_id"], str) or not row["job_id"]
                or row["job_id"] in ids
                or type(row["source_copy_count"]) is not int or row["source_copy_count"] < 1
                or any(not isinstance(row[key], str)
                       or re.fullmatch(r"[a-f0-9]{64}", row[key]) is None
                       for key in ("canonical_sha256", "source_copies_sha256"))):
            raise ValueError("Invalid public job/source-copy inventory row")
        ids.add(row["job_id"])
        total_sources += row["source_copy_count"]
    if (total_sources != inventory["source_copy_count"]
            or _inventory_digest(inventory["jobs"]) != inventory["sha256"]):
        raise ValueError("Public job/source-copy inventory checksum mismatch")
    return inventory


def collect_public_job_inventory(get_json, profile_id):
    """Hash canonical jobs and their source copies through public profile-scoped read routes."""
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("Job inventory requires a profile ID")
    encoded_profile = quote(profile_id, safe="")
    listed = get_json("/jobs?profile_id=" + encoded_profile)
    rows = listed.get("jobs") if isinstance(listed, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("Public job inventory requires at least one profile-scoped job")
    job_ids = [row.get("id") for row in rows if isinstance(row, dict)]
    if len(job_ids) != len(rows) or any(not isinstance(value, str) or not value for value in job_ids):
        raise ValueError("Public job listing contains an invalid job identity")
    if len(set(job_ids)) != len(job_ids):
        raise ValueError("Public job listing contains duplicate canonical identities")
    inventory_rows = []
    for job_id in sorted(job_ids):
        detail = get_json("/jobs/" + quote(job_id, safe="") + "?profile_id=" + encoded_profile)
        if not isinstance(detail, dict) or detail.get("id") != job_id:
            raise ValueError("Public canonical job detail changed identity")
        if any(field not in detail for field in CANONICAL_JOB_FIELDS):
            raise ValueError("Public canonical job detail is missing stable fields")
        routes = detail.get("routes")
        postings = detail.get("postings")
        if not isinstance(routes, list) or not routes or not isinstance(postings, list) or not postings:
            raise ValueError("Public job is missing application routes or source copies")
        canonical = {"job_id": job_id,
                     "fields": {field: detail[field] for field in CANONICAL_JOB_FIELDS},
                     "routes": sorted(({field: route.get(field) for field in JOB_ROUTE_FIELDS}
                                       for route in routes),
                                      key=lambda route: (str(route["provider"]), str(route["tenant"]),
                                                         str(route["requisition"]), str(route["id"])))}
        source_copies = []
        for posting in postings:
            if not isinstance(posting, dict) or any(field not in posting for field in SOURCE_COPY_FIELDS):
                raise ValueError("Public posting source copy is missing stable identity fields")
            source_copies.append({**{field: posting[field] for field in SOURCE_COPY_FIELDS},
                                  "raw_sha256": _inventory_digest(posting.get("raw"))})
        source_copies.sort(key=lambda row: (str(row["source"]), str(row["source_job_id"]),
                                            str(row["source_url"])))
        inventory_rows.append({"job_id": job_id,
                               "canonical_sha256": _inventory_digest(canonical),
                               "source_copy_count": len(source_copies),
                               "source_copies_sha256": _inventory_digest(source_copies)})
    result = {"schema_version": JOB_INVENTORY_VERSION, "profile_id": profile_id,
              "job_count": len(inventory_rows),
              "source_copy_count": sum(row["source_copy_count"] for row in inventory_rows),
              "jobs": inventory_rows, "sha256": _inventory_digest(inventory_rows)}
    return validate_public_job_inventory(result)


def compare_public_job_inventory(baseline, before_restart, after_restart):
    """Compare an explicit UI baseline with public readbacks around service restart."""
    if "job_inventory" not in baseline:
        return {"result": "NOT_RUN",
                "reason": "Baseline has no canonical public job/source-copy inventory"}
    expected = validate_public_job_inventory(baseline["job_inventory"])
    before = validate_public_job_inventory(before_restart)
    after = validate_public_job_inventory(after_restart)
    if not (expected["profile_id"] == before["profile_id"] == after["profile_id"]):
        raise ValueError("Public job/source inventory profile changed across restart")
    if expected["sha256"] != before["sha256"]:
        raise ValueError("Pre-restart public job/source inventory differs from UI baseline")
    if before["sha256"] != after["sha256"]:
        raise ValueError("Public job/source inventory changed across restart")
    return {"result": "PASS", "profile_id": before["profile_id"],
            "job_count": before["job_count"], "source_copy_count": before["source_copy_count"],
            "sha256": before["sha256"],
            "scope": "Same-profile canonical jobs, application routes, posting source identities and source-copy payload hashes via public GET /jobs readback"}


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
    config_files = resolve_compose_files()
    initial_targets = preflight_recovery_targets(project_name, port, config_files=config_files)
    api = f"http://127.0.0.1:{port}"
    actions = []
    job_inventory_result = ({"result": "PENDING"} if "job_inventory" in baseline
                            else compare_public_job_inventory(baseline, None, None))
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
        job_inventory_before = None
        if "job_inventory" in baseline:
            job_inventory_before = collect_public_job_inventory(
                lambda path: request("GET", path), baseline["profile_id"])
            compare_public_job_inventory(baseline, job_inventory_before, job_inventory_before)
        health_before = until(lambda: healthy_containers(initial_targets))
        services_to_restore = tuple(
            service for service in RECOVERY_RESTART_SERVICES
            if initial_targets[service]["status"] == "running"
        )
        try:
            compose(project_name, port, "stop", "worker", "source-worker", "company-discovery-worker",
                    "document-worker", "ai-worker", "redis", config_files=config_files)
            profile = dict(PROFILE, id="recovery-fictional-" + uuid4().hex[:12])
            request("PUT", "/profiles", json=profile)
            queued = request("POST", "/discovery", json={"profile_id": profile["id"]})
            assert queued["state"] == "QUEUED"
            degraded = client.get("/readiness")
            assert degraded.status_code == 503 and degraded.json()["checks"]["queue"] is False
            def api_is_unhealthy():
                probe = container_health(initial_targets["api"]["container_id"])
                return probe if probe == {"status": "running", "health": "unhealthy"} else False
            health_during_outage = until(api_is_unhealthy)
            actions.append({"action": "Redis disconnected; discovery persisted", "result": "PASS", "operation_id": queued["operation_id"]})
            # --no-deps prevents Compose from helpfully restoring Redis early.
            compose(project_name, port, "up", "-d", "--no-deps", "--no-build", "worker",
                    config_files=config_files)
            initial_worker_restart_count = initial_targets["worker"]["restart_count"]
            until(lambda: worker_restart_count(project_name, port, config_files=config_files)
                  > initial_worker_restart_count)
            compose(project_name, port, "start", "redis", config_files=config_files)
            compose(project_name, port, "start", "worker", "source-worker", "company-discovery-worker",
                    "document-worker", "ai-worker", config_files=config_files)
            until(lambda: request("GET", "/operations/" + queued["operation_id"])["state"] == "COMPLETED")
            until(lambda: client.get("/readiness").status_code == 200)
            actions.append({"action": "Worker supervision and durable outbox recovered after Redis returned", "result": "PASS"})
            restart_recovery_services(project_name, port, config_files=config_files)
            until(lambda: client.get("/readiness").status_code == 200)
            application_path = verify_recovered_application(request, baseline)
            if job_inventory_before is not None:
                job_inventory_after = collect_public_job_inventory(
                    lambda path: request("GET", path), baseline["profile_id"])
                job_inventory_result = compare_public_job_inventory(
                    baseline, job_inventory_before, job_inventory_after)
                actions.append({"action": "Same-profile canonical jobs, application routes, posting source identities and source-copy payload hashes matched UI baseline and post-restart public readback",
                                "result": "PASS", "job_count": job_inventory_result["job_count"],
                                "source_copy_count": job_inventory_result["source_copy_count"],
                                "sha256": job_inventory_result["sha256"]})
            else:
                actions.append({"action": "Canonical public jobs/source-copy persistence across restart",
                                "result": "NOT_RUN",
                                "reason": job_inventory_result["reason"]})
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
            receipt = json.loads(compose(project_name, port, "exec", "-T", "api", "python", "-c", program,
                                         baseline["application_id"], config_files=config_files))
            assert receipt == baseline["confirmation"] and receipt["submission_count"] == 1
            # Compose up can replace a stopped worker. Reverify the complete
            # project/service/config-file identity before inspecting fresh IDs.
            recovered_targets = preflight_recovery_targets(
                project_name, port, config_files=config_files,
            )
            health_after = until(lambda: healthy_containers(recovered_targets))
            from cli import local_persistence_acceptance as persistence
            persistence_result = persistence.compare_baseline(
                baseline, persistence.observe_http(client, baseline)
                if 'local_persistence' in baseline else None)
            if 'implementation_progress' in baseline:
                progress_result = validate_implementation_progress(baseline['implementation_progress'])
                actions.append({
                    "action": "Implementation progress journal and authoritative checkpoint survived restart",
                    "result": "PASS",
                    "checkpoint": progress_result.get("last_safe_checkpoint"),
                })
            actions.append({"action": "All services restarted; original application, operation and independent receipt survived without another submission", "result": "PASS"})
        finally:
            compose(project_name, port, "start", *services_to_restore, config_files=config_files)
    evidence = {"result": "PASS", "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": [sys.executable, "-m", "cli.compose_recovery", "--baseline", str(baseline_path),
                    "--evidence", str(evidence_path), "--project-name", project_name,
                    "--port", str(port)],
        "compose_target": {"project_name": project_name, "port": port,
                           "compose_files": [str(path) for path in config_files],
                           "compose_config_files_label": expected_config_files_label(config_files),
                           "project_directory": str(REPOSITORY_ROOT.resolve()),
                           "profile": "fixture", "services": list(RECOVERY_RESTART_SERVICES)},
        "target": "Project-only PostgreSQL/Redis/RQ/mock persistence and outage recovery", "actions": actions,
        "application_id": baseline["application_id"], "external_submission_enabled": False,
        "local_persistence": persistence_result,
        "public_job_inventory_recovery": job_inventory_result,
        "implementation_progress": (
            validate_implementation_progress(baseline["implementation_progress"])
            if "implementation_progress" in baseline else None
        ),
        "container_health": {"before": health_before, "redis_outage": {"api": health_during_outage},
                             "after": health_after}}
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
