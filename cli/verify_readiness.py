"""Report live dependency readiness from the API's observed runtime checks."""
from __future__ import annotations

import argparse
import json
import math
import sys

import httpx


_QUEUE_LABELS = (
    ("mjobs-application", "Application worker"),
    ("mjobs-company-discovery", "Company discovery worker"),
    ("mjobs-discovery", "Discovery worker"),
    ("mjobs-document", "Document worker"),
)
MAX_TIMEOUT_SECONDS = 30.0


def validate_timeout(value):
    """Bound network waiting time and reject non-finite float values."""
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a number from 0 through 30 seconds") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be a number from 0 through 30 seconds")
    return timeout


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


def run(base_url="http://127.0.0.1:18700", *, timeout=5.0):
    """Print a bounded readiness report; return nonzero unless all checks pass."""
    try:
        with httpx.Client(base_url=base_url, timeout=timeout, trust_env=False) as client:
            response = client.get("/readiness")
            try:
                payload = response.json()
            except ValueError:
                payload = None
            http_status = response.status_code
    except httpx.HTTPError:
        payload = None
        http_status = None
    result = evaluate_readiness(payload, http_status=http_status)
    print("Job Platform Verification")
    for name, status in result["checks"].items():
        print(f"[{status}] {name}")
    return 0 if result["ready"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:18700")
    parser.add_argument("--timeout", type=validate_timeout, default=5.0)
    from cli.verification_checks import run_connected, exit_status, validate_connected_timeout
    parser.add_argument("--connected", action="store_true", help="Execute isolated synthetic flows; separate from deployment readiness")
    parser.add_argument("--fixture", action="store_true", help="Explicitly admit a newly owned temporary FIXTURE/API/database")
    parser.add_argument("--connected-timeout", type=validate_connected_timeout, default=120,
                        help="Total connected child deadline, 1..120 seconds")
    parser.add_argument("--json", action="store_true", help="Emit current connected report as JSON")
    args = parser.parse_args(argv)
    if args.connected:
        if not args.fixture:
            parser.error("--connected requires explicit --fixture admission")
        if args.api != "http://127.0.0.1:18700":
            parser.error("--connected owns its fixture target; --api cannot be used")
        result = run_connected(fixture=True, timeout=args.connected_timeout)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Job Platform Verification — isolated synthetic FIXTURE (not deployment readiness)")
            print(f"Invocation: {result['invocation_id']} Revision: {result['revision']} Inputs: {result['source_sha256']}")
            for name, row in result["checks"].items():
                print(f"[{row['status']}] {name}: {row['reason']}")
            cleanup = result.get("cleanup", {"status": "NOT_RUN", "reason": "no owned execution admitted"})
            print(f"[{cleanup['status']}] Isolated cleanup: {cleanup['reason']}")
        return exit_status(result)
    if args.fixture or args.json or args.connected_timeout != 120:
        parser.error("--fixture, --json and --connected-timeout require --connected")
    return run(args.api, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
