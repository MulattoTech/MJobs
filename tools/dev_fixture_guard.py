"""Bounded, disposable MJobs Compose fixtures; never targets production stacks.

Only projects created by this tool carry its ownership label and are eligible for
cleanup. Unlabelled legacy fixtures, personal stacks and production are untouched.
"""
import argparse
import base64
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yml"
OVERRIDE = ROOT / "compose.dev-fixture.yml"
PATTERN = re.compile(r"mjobs-fixture-[0-9a-f]{12}\Z")
TOKEN_LABEL = "com.mjobs.dev.guard"
EXPIRY_LABEL = "com.mjobs.dev.expires"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
SERVICES = frozenset({
    "postgres", "redis", "migrate", "api", "mock", "worker", "scheduler",
    "source-worker", "company-discovery-worker", "ai-worker", "document-worker",
})
VOLUMES = frozenset({
    "database", "queue", "documents", "mock-receipts",
    "runtime-database", "migration-backups",
})
NETWORKS = frozenset({"private", "ui", "source-egress"})


def docker(*args, env=None, timeout=90):
    try:
        result = subprocess.run(
            ["docker", *args], cwd=ROOT, env=env,
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Docker command could not complete") from exc
    if result.returncode:
        raise RuntimeError(
            f"Docker command failed ({result.returncode}): "
            + result.stderr[-1000:].strip()
        )
    return result.stdout.strip()


def containers(project=None, running=False):
    filters = ["--filter", f"label={PROJECT_LABEL}"]
    if project is not None:
        filters = ["--filter", f"label={PROJECT_LABEL}={project}"]
    ids = docker("ps", "-q" if running else "-aq", *filters).split()
    if not ids:
        return []
    lines = docker("inspect", "--format", "{{json .Config.Labels}}", *ids).splitlines()
    if len(lines) != len(ids):
        raise RuntimeError("Container inspection count mismatch")
    labels = [json.loads(line) for line in lines]
    if any(not isinstance(row, dict) for row in labels):
        raise RuntimeError("Container labels were unavailable")
    return labels


def project_resources(project):
    return {
        "containers": containers(project),
        "volumes": docker("volume", "ls", "-q", "--filter",
                          f"label={PROJECT_LABEL}={project}").splitlines(),
        "networks": docker("network", "ls", "-q", "--filter",
                           f"label={PROJECT_LABEL}={project}").splitlines(),
    }


def verify_owned(project, token):
    if not PATTERN.fullmatch(project) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Invalid disposable project identity")
    rows = containers(project)
    if not rows:
        raise RuntimeError("No labelled containers to establish fixture ownership")
    for row in rows:
        if (row.get(PROJECT_LABEL) != project or row.get(TOKEN_LABEL) != token
                or row.get(SERVICE_LABEL) not in SERVICES):
            raise RuntimeError("Refusing cleanup: project contains an unowned container")
    # Compose-created volumes and networks are scoped to the exact generated
    # project. Refuse additional resources that might belong to another operator.
    names = docker("volume", "ls", "-q", "--filter",
                   f"label={PROJECT_LABEL}={project}").splitlines()
    if any(name not in {f"{project}_{part}" for part in VOLUMES}
           for name in names):
        raise RuntimeError("Refusing cleanup: unexpected project volume")
    network_ids = docker("network", "ls", "-q", "--filter",
                         f"label={PROJECT_LABEL}={project}").split()
    if network_ids:
        network_names = docker("network", "inspect", "--format",
                               "{{.Name}}", *network_ids).splitlines()
        if any(name not in {f"{project}_{part}" for part in NETWORKS}
               for name in network_names):
            raise RuntimeError("Refusing cleanup: unexpected project network")
    return True


def compose_args(project):
    if not PATTERN.fullmatch(project):
        raise ValueError("Not an isolated MJobs fixture project")
    return ("compose", "--file", str(COMPOSE), "--file", str(OVERRIDE),
            "--project-directory", str(ROOT), "--project-name", project,
            "--profile", "fixture")


def cleanup(project, token, env=None):
    verify_owned(project, token)
    # Compose interpolates its config even for "down". Supply non-production,
    # cleanup-only placeholders if the independent watchdog has no parent env.
    # Never read or reuse production secrets when reaping an expired fixture.
    rows = containers(project)
    expiries = {row.get(EXPIRY_LABEL) for row in rows}
    if len(expiries) != 1 or not next(iter(expiries)):
        raise RuntimeError("Refusing cleanup: ambiguous fixture lease")
    compose_env = dict(os.environ if env is None else env)
    compose_env.update({
        "MJOBS_DEV_GUARD_TOKEN": token,
        "MJOBS_DEV_GUARD_EXPIRES": next(iter(expiries)),
    })
    if env is None:
        compose_env["POSTGRES_PASSWORD"] = "fixture-cleanup-only"
        compose_env["MJOBS_CREDENTIAL_ENCRYPTION_KEY"] = "fixture-cleanup-only"
    compose_env.pop("COMPOSE_FILE", None)
    # Only newly generated, token-labelled projects reach this call. Preserve
    # images/build cache; delete the disposable project's own test volumes.
    docker(*compose_args(project), "down", "--remove-orphans", "--volumes",
           env=compose_env, timeout=180)


def inventory():
    grouped = defaultdict(list)
    for row in containers():
        project = row.get(PROJECT_LABEL, "")
        if PATTERN.fullmatch(project):
            grouped[project].append(row)
    result = []
    for project, rows in sorted(grouped.items()):
        tokens = {r.get(TOKEN_LABEL) for r in rows}
        expiries = {r.get(EXPIRY_LABEL) for r in rows}
        managed = (len(tokens) == 1 and len(expiries) == 1
                   and next(iter(tokens)) is not None
                   and next(iter(expiries)) is not None)
        result.append({
            "project": project, "containers": len(rows), "managed": managed,
            "expires": next(iter(expiries)) if managed else None,
            "token": next(iter(tokens)) if managed else None,
        })
    return result


def sweep(apply=False):
    now = int(time.time())
    for row in inventory():
        if not row["managed"]:
            continue
        try:
            expiry = int(row["expires"])
        except (TypeError, ValueError):
            continue
        if expiry > now:
            continue
        print(f"Expired managed fixture: {row['project']} ({row['containers']} containers)")
        if apply:
            cleanup(row["project"], row["token"])
            print("Removed only token-verified disposable fixture")


def choose_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def fixture_environment(project, token, expiry, port):
    env = os.environ.copy()
    # Never reuse production credentials for isolated synthetic test data.
    env.update({
        "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_PROFILES": "fixture",
        "MJOBS_PORT": str(port),
        "MJOBS_OPERATION_MODE": "FIXTURE",
        "MJOBS_COMPOSE_MOCK": "1",
        "MJOBS_DEV_GUARD_TOKEN": token,
        "MJOBS_DEV_GUARD_EXPIRES": str(expiry),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "MJOBS_CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(
            secrets.token_bytes(32)).decode("ascii"),
    })
    # Explicit -f flags are used; never inherit a caller's other Compose stack.
    env.pop("COMPOSE_FILE", None)
    return env


def spawn_watchdog(project, token, expiry):
    log_dir = ROOT / "outputs" / "dev-fixture-guard"
    log_dir.mkdir(parents=True, exist_ok=True)
    output = open(log_dir / f"{project}.watchdog.log", "ab")
    kwargs = {"cwd": ROOT, "stdin": subprocess.DEVNULL,
              "stdout": output, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "tools.dev_fixture_guard", "watchdog",
             project, token, str(expiry)], **kwargs)
    finally:
        output.close()


def run(args):
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("Supply a verification command after --")
    # Expired fixtures owned by this utility are safely reaped on the next run.
    sweep(apply=True)
    running = containers(running=True)
    existing = {row.get(PROJECT_LABEL) for row in running
                if PATTERN.fullmatch(row.get(PROJECT_LABEL, ""))}
    if len(existing) >= args.max_fixtures:
        raise RuntimeError(
            f"Fixture limit reached ({len(existing)} running, "
            f"limit {args.max_fixtures}); inspect audit before starting another")
    project = f"mjobs-fixture-{secrets.token_hex(6)}"
    if any(project_resources(project).values()):
        raise RuntimeError("Generated fixture identity collides with existing resources")
    token = secrets.token_hex(16)
    expiry = int(time.time()) + args.ttl_minutes * 60
    port = args.port or choose_port()
    env = fixture_environment(project, token, expiry, port)
    watchdog = spawn_watchdog(project, token, expiry)
    cleaned = False
    try:
        print(f"Starting disposable {project} on 127.0.0.1:{port}", flush=True)
        build_flag = "--build" if args.build else "--no-build"
        docker(*compose_args(project), "up", "--detach", "--wait",
               "--wait-timeout", "180", build_flag, env=env, timeout=600)
        # Keep the bounded verification command attached. This process's exit
        # status is preserved, including a timeout or interrupted session.
        result = subprocess.run(command, cwd=ROOT, env=env,
                                timeout=args.ttl_minutes * 60, check=False)
        return result.returncode
    finally:
        try:
            cleanup(project, token, env=env)
            cleaned = True
            print(f"Disposed fixture {project}", flush=True)
        except (RuntimeError, ValueError) as exc:
            print(f"Cleanup not proven; watchdog/sweep can retry: {exc}",
                  file=sys.stderr, flush=True)
        if cleaned and watchdog.poll() is None:
            watchdog.terminate()


def watchdog(project, token, expiry):
    seconds = int(expiry) - int(time.time())
    if seconds > 0:
        time.sleep(seconds)
    try:
        cleanup(project, token)
    except (RuntimeError, ValueError) as exc:
        # Normal foreground completion has already removed the project.
        print(f"Watchdog cleanup skipped: {exc}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    subs.add_parser("audit", help="Read-only inventory of fixture projects")
    s = subs.add_parser("sweep", help="Show/remove expired, tool-owned projects only")
    s.add_argument("--apply", action="store_true")
    r = subs.add_parser("run", help="Run a bounded disposable Docker fixture")
    r.add_argument("--ttl-minutes", type=int, default=60)
    r.add_argument("--max-fixtures", type=int, default=1)
    r.add_argument("--port", type=int)
    r.add_argument("--build", action="store_true")
    r.add_argument("command", nargs=argparse.REMAINDER)
    w = subs.add_parser("watchdog", help=argparse.SUPPRESS)
    w.add_argument("project")
    w.add_argument("token")
    w.add_argument("expiry", type=int)
    args = parser.parse_args(argv)
    if args.action == "audit":
        for entry in inventory():
            print(json.dumps({k: v for k, v in entry.items() if k != "token"}))
        return 0
    if args.action == "sweep":
        sweep(apply=args.apply)
        return 0
    if args.action == "watchdog":
        watchdog(args.project, args.token, args.expiry)
        return 0
    if not 1 <= args.ttl_minutes <= 240:
        parser.error("TTL must be 1-240 minutes")
    if not 1 <= args.max_fixtures <= 3:
        parser.error("Fixture limit must be 1-3")
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error("Port must be 1024-65535")
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Fixture guard: {exc}", file=sys.stderr)
        sys.exit(1)
