"""Pull-based, fail-closed MJobs personal-preview deployment controller.

Run from a root-owned systemd oneshot service on the dedicated Ubuntu VM.
Only previously validated main commits can be promoted. No GitHub credentials,
public inbound port, PAT, shell interpretation or real job submission is used.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.request import Request, urlopen

REPO = "https://github.com/MulattoTech/MJobs.git"
API = "https://api.github.com/repos/MulattoTech/MJobs"
PROJECT = "mjobs-personal"
HOME = Path("/var/lib/mjobs-cd")
SECRETS = Path("/etc/mjobs/personal.env")
OLD_STATE = HOME / "deployed.json"
CHECKS = ("Deployment / candidate", "CI / required",
          "Integration / PostgreSQL and Redis", "Security / secret scan")
WRITERS = ("api", "worker", "source-worker", "document-worker", "ai-worker",
           "company-discovery-worker", "scheduler")
PUBLIC_HOST = "mjobs.m-techlabs.com"


class DeployError(RuntimeError):
    pass


def log(message: str):
    print(f"[mjobs-cd] {message}", flush=True)


def https_json(url: str) -> dict:
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "mjobs-ubuntu-personal-deploy",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def latest_main() -> str:
    sha = https_json(API + "/branches/main")["commit"]["sha"].lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeployError("GitHub returned an invalid main commit")
    return sha


def current_checks(sha: str) -> dict[str, str]:
    data = https_json(API + f"/commits/{sha}/check-runs?per_page=100")
    if data.get("total_count", 0) > 100:
        raise DeployError("Refusing ambiguous paginated check results")
    latest = {}
    for row in data.get("check_runs", []):
        name = row.get("name")
        if name not in CHECKS or row.get("app", {}).get("slug") != "github-actions":
            continue
        order = (row.get("started_at") or "", row.get("id") or 0)
        if name not in latest or order > latest[name][0]:
            latest[name] = (order, row)
    return {name: ("MISSING" if name not in latest else
                   "PENDING" if latest[name][1].get("status") != "completed" else
                   str(latest[name][1].get("conclusion") or "UNKNOWN").upper())
            for name in CHECKS}


def run(arguments: list[str], *, cwd: Path | None = None,
        environment: dict[str, str] | None = None, timeout=1800):
    result = subprocess.run(arguments, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
    if result.returncode:
        # Never echo stderr: Docker and provider tools can include private values.
        raise DeployError(f"{arguments[0]} step failed (exit {result.returncode})")
    return result.stdout.strip()


def environment(sha: str, image: str) -> dict[str, str]:
    return {**os.environ, "MJOBS_PORT": "18701", "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_COMPOSE_MOCK": "0", "MJOBS_IMAGE": image,
            "MJOBS_DEPLOY_SHA": sha, "MJOBS_PUBLIC_HOST": PUBLIC_HOST}


def compose(source: Path, sha: str, image: str, *actions: str, timeout=600):
    return run(["docker", "compose", "-p", PROJECT, "-f",
                str(source / "compose.yml"), *actions],
               cwd=source, environment=environment(sha, image), timeout=timeout)


def fingerprint(source: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted((source / "migrations").rglob("*.py"))
    paths.append(source / "alembic.ini")
    for path in paths:
        digest.update(path.relative_to(source).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def atomic_json(destination: Path, payload: dict):
    temporary = destination.with_suffix(".new")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(payload, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)


def poll_ready(expected_sha: str | None = None, timeout_seconds: int = 150):
    from urllib.error import HTTPError, URLError
    limit = time.monotonic() + timeout_seconds
    while time.monotonic() < limit:
        try:
            request = Request("http://127.0.0.1:18701/ready",
                              headers={"Host": "127.0.0.1"})
            with urlopen(request, timeout=4) as response:
                ready = json.load(response)
            if ready.get("status") != "ready":
                raise DeployError("Application readiness is degraded")
            if expected_sha:
                with urlopen("http://127.0.0.1:18701/__deployment", timeout=4) as response:
                    marker = json.load(response)
                if marker.get("commit") != expected_sha or marker.get("operation_mode") != "PERSONAL":
                    raise DeployError("Running revision or personal mode mismatched")
            return
        except (HTTPError, URLError, OSError, TimeoutError, ValueError, DeployError):
            time.sleep(3)
    raise DeployError("Application did not reach ready state at expected revision")


def restore_previous(previous: dict):
    source = Path(previous["source"])
    log("Restoring prior image without touching PostgreSQL or saved documents")
    compose(source, previous["sha"], previous["image"], "up", "-d", "--no-build", timeout=240)
    poll_ready(timeout_seconds=120)


def perform_update(sha: str, previous: dict):
    source = HOME / "releases" / sha
    if source.exists():
        raise DeployError("Existing release directory requires operator reconciliation")
    stage = source.with_name(sha + ".staging")
    if stage.exists():
        raise DeployError("Incomplete staged release requires operator reconciliation")
    log("Fetching exact validated source")
    run(["git", "clone", "--depth", "1", "--single-branch", "--branch", "main",
         REPO, str(stage)], timeout=180)
    if run(["git", "rev-parse", "HEAD"], cwd=stage).lower() != sha:
        raise DeployError("Main changed while staging; will reconsider on next scheduled check")
    if not SECRETS.is_file():
        raise DeployError("Protected original environment is missing")
    shutil.copyfile(SECRETS, stage / ".env")
    os.chmod(stage / ".env", 0o600)
    stage.rename(source)
    if fingerprint(source) != fingerprint(Path(previous["source"])):
        raise DeployError("Migration history changed: no unattended database migration permitted")
    image = "mjobs-local:sha-" + sha
    log("Building immutable candidate image (Dockerfile also tests the frontend)")
    run(["docker", "build", "--file", "infra/docker/Dockerfile",
         "--tag", image, "."], cwd=source, timeout=3600)
    if latest_main() != sha:
        raise DeployError("New main commit superseded the candidate before rollout")
    if any(value != "SUCCESS" for value in current_checks(sha).values()):
        raise DeployError("Exact-head validation changed before rollout")

    oldsource = Path(previous["source"])
    stopped = False
    try:
        log("Quiescing personal-workspace writers without stopping PostgreSQL")
        compose(oldsource, previous["sha"], previous["image"], "stop", *WRITERS, timeout=240)
        stopped = True
        archive = HOME / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") +
                                       "-" + previous["sha"][:12])
        log("Creating and verifying existing MJobs integrity-checked backup")
        run([sys.executable, "-m", "cli.local_backup", "backup",
             "--project", PROJECT, "--archive", str(archive)],
            cwd=oldsource, environment=environment(previous["sha"], previous["image"]), timeout=900)
        run([sys.executable, "-m", "cli.local_backup", "verify",
             "--archive", str(archive)], cwd=oldsource, timeout=180)
        log("Starting validated candidate with original data volumes")
        compose(source, sha, image, "up", "-d", "--no-build", timeout=600)
        poll_ready(expected_sha=sha)
    except Exception:
        if stopped:
            try:
                restore_previous(previous)
            except Exception:
                log("CRITICAL: automatic rollback failed; preserve data, inspect services immediately")
        raise
    atomic_json(OLD_STATE, {"sha": sha, "source": str(source), "image": image,
                            "deployed_at": datetime.now(timezone.utc).isoformat()})
    log("READY: deployed SHA " + sha)


def main() -> int:
    if os.geteuid() != 0:
        log("Root-owned service is required for protected Docker and backup access")
        return 2
    for path in (HOME, HOME / "releases", HOME / "backups"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    with (HOME / "deploy.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Previous deployment check is still active; not overlapping")
            return 0
        try:
            if not OLD_STATE.exists():
                raise DeployError("No reviewed baseline; bootstrap deployed.json before enabling")
            previous = json.loads(OLD_STATE.read_text(encoding="utf-8"))
            if not (re.fullmatch(r"[0-9a-f]{40}", previous.get("sha", "")) and
                    Path(previous.get("source", "")).is_dir() and
                    previous.get("image") == "mjobs-local:sha-" + previous["sha"]):
                raise DeployError("Deployment state is invalid")
            sha = latest_main()
            if sha == previous["sha"]:
                return 0
            checks = current_checks(sha)
            if any(value != "SUCCESS" for value in checks.values()):
                log("Main is not a verified deployment candidate; retaining prior image")
                return 0
            perform_update(sha, previous)
            return 0
        except Exception as error:
            # Do not expose credentials, provider errors, private document paths or tracebacks.
            log("Deployment blocked: " + str(error) if isinstance(error, DeployError)
                else "Deployment blocked by an unexpected failure")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
