"""Report live dependency readiness from the API's observed runtime checks."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx

from services.ai.providers import AIProviderError
from services.ai.providers.transport import request_guard


_QUEUE_LABELS = (
    ("mjobs-application", "Application worker"),
    ("mjobs-company-discovery", "Company discovery worker"),
    ("mjobs-discovery", "Discovery worker"),
    ("mjobs-document", "Document worker"),
)
MAX_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 64 * 1024


def validate_timeout(value):
    """Bound network waiting time and reject non-finite float values."""
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a number from 0 through 30 seconds") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be a number from 0 through 30 seconds")
    return timeout


def validate_api_origin(value):
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("API port must be valid") from error
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or port is not None and not 0 < port <= 65535
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise ValueError("API must be an HTTP(S) origin without credentials, query or path")
    return value.rstrip("/")


def _result(value):
    if type(value) is bool:
        return "PASS" if value else "FAIL"
    return "NOT_RUN"


def evaluate_readiness(payload, *, http_status=200):
    """Require explicit live evidence for config, schema, services, and queues."""
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, dict):
        checks = {}
    workers = checks.get("workers")
    if not isinstance(workers, dict):
        workers = {}
    postgres = _result(checks.get("database"))
    if (postgres == "PASS"
            and (not isinstance(payload.get("db_backend"), str)
                 or payload["db_backend"].casefold() != "postgresql")):
        postgres = "NOT_RUN"
    outcomes = {
        "Configuration valid": _result(checks.get("configuration")),
        "Database schema current": _result(checks.get("schema")),
        "PostgreSQL reachable": postgres,
        "Redis reachable": _result(checks.get("queue")),
        "Scheduler healthy": _result(checks.get("scheduler")),
    }
    for queue, label in _QUEUE_LABELS:
        outcomes[label] = _result(workers.get(queue))
    outcomes["All required workers"] = _result(checks.get("worker"))
    ready = (http_status == 200 and isinstance(payload, dict) and payload.get("status") == "ready"
             and all(value == "PASS" for value in outcomes.values()))
    return {"ready": ready, "checks": outcomes}


def _read_runtime_json(base_url, path, deadline):
    """Bound DNS, TCP, TLS, headers and streamed bytes with one deadline."""
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None
        # A fresh client keeps each guard's transport/socket ownership local.
        with httpx.Client(base_url=base_url, timeout=remaining, trust_env=False,
                          follow_redirects=False) as client, request_guard(client, deadline=deadline) as guard:
            with client.stream("GET", path, headers={"Accept-Encoding": "identity"},
                               extensions={"trace": guard.trace}) as response:
                guard.bind_response(response)
                status = response.status_code
                if status != 200 or response.headers.get("content-encoding", "identity").lower() != "identity":
                    return None, status
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                    return None, status
                body = bytearray()
                for chunk in response.iter_raw():
                    guard.check()
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        return None, status
                    body.extend(chunk)
                guard.check()
                return json.loads(body), status
    except (AIProviderError, httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, RecursionError):
        return None, None


def collect_readiness(base_url, *, timeout=5.0, include_identity=False):
    """Read live health without returning response text or private fields."""
    base_url = validate_api_origin(base_url)
    deadline = time.monotonic() + validate_timeout(timeout)
    payload, http_status = _read_runtime_json(base_url, "/readiness", deadline)
    identity = None
    if include_identity:
        value, status = _read_runtime_json(base_url, "/__deployment", deadline)
        if (status == 200 and isinstance(value, dict)
                and isinstance(value.get("commit"), str)
                and re.fullmatch(r"[0-9a-f]{40}", value["commit"])
                and isinstance(value.get("version"), str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", value["version"])
                and isinstance(value.get("operation_mode"), str)
                and value["operation_mode"] in {"PERSONAL", "FIXTURE"}):
            identity = {name: value[name] for name in ("commit", "version", "operation_mode")}
    result = evaluate_readiness(payload, http_status=http_status)
    result.update(observed_at=datetime.now(timezone.utc).isoformat(),
                  scope="observed API runtime readiness", api_origin=base_url)
    if include_identity:
        result["deployment"] = identity
        result["checks"]["Deployment identity"] = "PASS" if identity is not None else "NOT_RUN"
        result["ready"] = result["ready"] and identity is not None
    return result


def run(base_url="http://127.0.0.1:18700", *, timeout=5.0):
    """Print a bounded readiness report; return nonzero unless all checks pass."""
    result = collect_readiness(base_url, timeout=timeout)
    print("Job Platform Verification")
    for name, status in result["checks"].items():
        print(f"[{status}] {name}")
    return 0 if result["ready"] else 1


def working_tree_clean():
    from cli.verification_checks import ROOT
    try:
        result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=ROOT, text=True, capture_output=True, timeout=5)
        return result.returncode == 0 and not result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False


def run_full(base_url, *, fixture=False, timeout=5.0, connected_timeout=120):
    """Combine fresh live observations and fresh owned functional execution."""
    from cli.verification_checks import run_connected, exit_status, source_identity
    if fixture is not True:
        raise ValueError("full verification requires explicit fixture admission")
    started = datetime.now(timezone.utc).isoformat()
    source_before = source_identity()
    clean_before = working_tree_clean()
    before = collect_readiness(base_url, timeout=timeout, include_identity=True)
    functional = run_connected(fixture=True, timeout=connected_timeout)
    after = collect_readiness(base_url, timeout=timeout, include_identity=True)
    source_after = source_identity()
    clean_after = working_tree_clean()
    clean = clean_before and clean_after
    # The child's own before/after hash only covers functional execution.
    # Bind it to the clean source around both live probes too: edits reverted
    # during the final probe must not qualify a different tested input tree.
    source_matches = source_before == source_after == (
        functional.get("revision"), functional.get("source_sha256"))
    deployment = after.get("deployment")
    identity_matches = (deployment is not None and before.get("deployment") == deployment
                        and deployment["commit"] == functional.get("revision")
                        and source_matches and clean)
    functional_status = exit_status(functional)
    cleanup_passed = functional.get("cleanup", {}).get("status") == "PASS"
    passed = (before["ready"] and after["ready"] and identity_matches
              and functional_status == 0 and cleanup_passed)
    return {
        "schema_version": 1, "mode": "FULL", "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if passed else "FAIL",
        "exit_code": 0 if passed else (2 if functional_status == 2 and before["ready"]
            and after["ready"] and identity_matches and cleanup_passed else 1),
        "runtime_before": before, "runtime_after": after, "functional": functional,
        "revision_identity": {
            "status": "PASS" if identity_matches else "FAIL", "working_tree_clean": clean,
            "source_before": {"revision": source_before[0], "source_sha256": source_before[1]},
            "source_after": {"revision": source_after[0], "source_sha256": source_after[1]},
            "functional_inputs_match": source_matches,
            "reason": ("same deployment and clean source revision/input digest before and after functional execution"
                       if identity_matches else "deployment/source revision or inputs missing, changed, different or local tree dirty"),
        },
    }


def print_functional(result):
    print(f"Invocation: {result['invocation_id']} Revision: {result['revision']} Inputs: {result['source_sha256']}")
    print(f"Execution: {result['execution_mode']}")
    for name, row in result["checks"].items():
        print(f"[{row['status']}] {name}: {row['reason']}")
    cleanup = result.get("cleanup", {"status": "NOT_RUN", "reason": "no owned execution admitted"})
    print(f"[{cleanup['status']}] Isolated cleanup: {cleanup['reason']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", type=validate_api_origin, default="http://127.0.0.1:18700")
    parser.add_argument("--timeout", type=validate_timeout, default=5.0)
    from cli.verification_checks import run_connected, exit_status, validate_connected_timeout
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--connected", action="store_true", help="Execute isolated synthetic flows, including one mock confirmation")
    modes.add_argument("--dry-run", action="store_true", help="Execute the full isolated pipeline and stop after form validation with zero submissions")
    modes.add_argument("--full", action="store_true", help="Observe live readiness and execute fresh isolated functional checks against the same source revision")
    parser.add_argument("--fixture", action="store_true", help="Explicitly admit a newly owned temporary FIXTURE/API/database")
    parser.add_argument("--connected-timeout", type=validate_connected_timeout, default=120,
                        help="Total connected child deadline, 1..120 seconds")
    parser.add_argument("--json", action="store_true", help="Emit the current full, dry-run or connected report as JSON")
    args = parser.parse_args(argv)
    if args.full:
        if not args.fixture:
            parser.error("--full requires explicit --fixture admission")
        result = run_full(args.api, fixture=True, timeout=args.timeout,
                          connected_timeout=args.connected_timeout)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Job Platform Verification")
            for label in ("runtime_before", "runtime_after"):
                runtime = result[label]
                print(f"Observed runtime ({label}, {runtime['observed_at']}):")
                for name, status in runtime["checks"].items():
                    print(f"[{status}] {name}")
                print("Deployment:", json.dumps(runtime["deployment"]))
            print("Executed functional checks — owned synthetic FIXTURE:")
            print_functional(result["functional"])
            identity = result["revision_identity"]
            print(f"[{identity['status']}] Revision identity: {identity['reason']}")
            print(f"[{result['status']}] Complete verification")
        return result["exit_code"]
    if args.connected or args.dry_run:
        if not args.fixture:
            parser.error("--connected and --dry-run require explicit --fixture admission")
        if args.api != "http://127.0.0.1:18700":
            parser.error("isolated execution owns its fixture target; --api cannot be used")
        result = run_connected(fixture=True, timeout=args.connected_timeout,
                               mode="DRY_RUN" if args.dry_run else "CONNECTED")
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Job Platform Verification — isolated synthetic FIXTURE (not deployment readiness)")
            print_functional(result)
        return exit_status(result)
    if args.fixture or args.json or args.connected_timeout != 120:
        parser.error("--fixture, --json and --connected-timeout require --connected, --dry-run or --full")
    return run(args.api, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
