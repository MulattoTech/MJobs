"""Wait for existing independent main-branch gates before accepting a deployable image.

A passing PR check does not count as a passing check on its merge commit.
"""
from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import errno
from http.client import HTTPException, IncompleteRead, RemoteDisconnected
import json
import math
import os
import re
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY = "MulattoTech/MJobs"
MAIN_GATES = (
    "CI / required",
    "Integration / PostgreSQL and Redis",
    "Security / secret scan",
)
MAX_TRANSIENT_RETRIES = 3
TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
CHECK_STATUSES = {"queued", "in_progress", "completed", "waiting", "requested", "pending"}
CHECK_CONCLUSIONS = {"success", "failure", "neutral", "cancelled", "skipped", "timed_out",
                     "action_required", "stale", "startup_failure"}


class LookupDeadlineExceeded(Exception):
    """The original gate deadline cannot admit another request or result."""


def retrieve_checks(sha: str, *, token: str = "", deadline: float | None = None) -> list[dict]:
    if deadline is None:
        deadline = time.monotonic() + 75  # At most five existing 15-second request budgets.
    checks = []
    for page in range(1, 6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LookupDeadlineExceeded()
        url = (f"https://api.github.com/repos/{REPOSITORY}/commits/{sha}"
               f"/check-runs?per_page=100&page={page}")
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "mjobs-deployment-verifier",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = "Bearer " + token
        with urlopen(Request(url, headers=headers), timeout=min(15, remaining)) as response:
            payload = json.load(response)
        if time.monotonic() >= deadline:
            raise LookupDeadlineExceeded()
        batch = payload.get("check_runs") if isinstance(payload, dict) else None
        if (not isinstance(batch, list) or len(batch) > 100
                or any(not isinstance(entry, dict) or entry.get("head_sha") != sha for entry in batch)):
            raise ValueError("Invalid GitHub checks response")
        checks.extend(batch)
        if len(batch) < 100:
            return checks
    raise ValueError("Too many check runs to validate without ambiguity")


def assess_checks(checks: list[dict], required=MAIN_GATES) -> dict[str, str]:
    """Use the highest validated run ID for each exact gate name and trusted app."""
    selected = {}
    states = {}
    for entry in checks:
        if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                or not isinstance(entry.get("app"), dict)):
            raise ValueError("Invalid GitHub check identity")
        name = entry.get("name")
        if name not in required or entry.get("app", {}).get("slug") != "github-actions":
            continue
        if (type(entry.get("id")) is not int or entry["id"] < 0
                or entry.get("started_at") is not None and not isinstance(entry["started_at"], str)
                or not isinstance(entry.get("status"), str) or entry["status"] not in CHECK_STATUSES
                or entry.get("conclusion") is not None and (
                    not isinstance(entry["conclusion"], str) or entry["conclusion"] not in CHECK_CONCLUSIONS)):
            raise ValueError("Invalid GitHub check state")
        identity = (name, entry["id"])
        state = (entry["status"], entry.get("conclusion"))
        if identity in states and states[identity] != state:
            raise ValueError("Conflicting GitHub check states")
        states[identity] = state
        # Queued reruns have no start timestamp; their newer ID must supersede
        # an older completed result instead of inheriting its success/failure.
        sort_key = entry["id"]
        if name not in selected or sort_key > selected[name][0]:
            selected[name] = (sort_key, entry)
    return {
        name: ("MISSING" if name not in selected
               else "PENDING" if selected[name][1].get("status") != "completed"
               else str(selected[name][1].get("conclusion") or "UNKNOWN").upper())
        for name in required
    }


def transient_lookup_failure(error: Exception) -> bool:
    if isinstance(error, HTTPError):
        return error.code in TRANSIENT_HTTP_STATUSES
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, (TimeoutError, ConnectionError, IncompleteRead, RemoteDisconnected)):
        return True
    if isinstance(reason, socket.gaierror):
        return reason.errno == socket.EAI_AGAIN
    return isinstance(reason, OSError) and reason.errno in {
        errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE,
        errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH,
    }


def retry_wait(error: Exception, retries: int) -> float | None:
    """Respect server backoff without copying header/body values into diagnostics."""
    delay = float(2 ** retries)
    value = error.headers.get("Retry-After") if isinstance(error, HTTPError) and error.headers else None
    if value is None:
        return max(delay, 60) if isinstance(error, HTTPError) and error.code == 429 else delay
    try:
        try:
            server_delay = float(value)
        except ValueError:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                return None
            server_delay = max(0, instant.timestamp() - time.time())
        if not math.isfinite(server_delay) or server_delay < 0:
            return None
        return max(delay, server_delay)
    except (TypeError, ValueError, OverflowError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=4200)
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("Expected a complete lowercase commit SHA")
    if not 0 <= args.timeout_seconds <= 7200 or not 5 <= args.poll_seconds <= 120:
        parser.error("Invalid bounded poll interval or deadline")
    deadline = time.monotonic() + args.timeout_seconds
    last = None
    retries = 0
    while time.monotonic() < deadline:
        try:
            status = assess_checks(retrieve_checks(args.sha, token=os.getenv("GH_TOKEN", ""), deadline=deadline))
        except LookupDeadlineExceeded:
            break
        except (HTTPError, URLError, OSError, ValueError, RecursionError, HTTPException) as error:
            diagnostic = type(error).__name__
            if isinstance(error, HTTPError):
                status_code = error.code if type(error.code) is int and 100 <= error.code <= 599 else "unknown"
                diagnostic += f" status={status_code}"
            delay = retry_wait(error, retries) if transient_lookup_failure(error) else None
            if isinstance(error, HTTPError):
                try:
                    error.close()
                except OSError:
                    # Do not retry with an unclosed response or expose its error.
                    delay = None
            print(f"GitHub gate lookup unavailable: {diagnostic}", file=sys.stderr, flush=True)
            if delay is None:
                return 2
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if retries >= MAX_TRANSIENT_RETRIES:
                return 2
            retries += 1
            print(f"Retrying read-only gate lookup ({retries}/{MAX_TRANSIENT_RETRIES})",
                  file=sys.stderr, flush=True)
            time.sleep(min(delay, remaining))
            continue
        if time.monotonic() >= deadline:
            break
        retries = 0
        if status != last:
            print(json.dumps({"commit": args.sha, "checks": status}, sort_keys=True), flush=True)
            last = status
        if all(value == "SUCCESS" for value in status.values()):
            return 0
        failed = {name: value for name, value in status.items()
                  if value not in {"MISSING", "PENDING", "SUCCESS"}}
        if failed:
            print("Deployment candidate blocked by failed exact-head checks", file=sys.stderr)
            return 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(args.poll_seconds, remaining))
    print("Deployment candidate blocked: gate verification deadline exhausted", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
