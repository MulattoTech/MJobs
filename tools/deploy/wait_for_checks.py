"""Wait for existing independent main-branch gates before accepting a deployable image.

A passing PR check does not count as a passing check on its merge commit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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


def retrieve_checks(sha: str, *, token: str = "") -> list[dict]:
    checks = []
    for page in range(1, 6):
        url = (f"https://api.github.com/repos/{REPOSITORY}/commits/{sha}"
               f"/check-runs?per_page=100&page={page}")
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "mjobs-deployment-verifier",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = "Bearer " + token
        with urlopen(Request(url, headers=headers), timeout=15) as response:
            payload = json.load(response)
        batch = payload.get("check_runs", [])
        if not isinstance(batch, list):
            raise ValueError("Invalid GitHub checks response")
        checks.extend(batch)
        if len(batch) < 100:
            return checks
    raise ValueError("Too many check runs to validate without ambiguity")


def assess_checks(checks: list[dict], required=MAIN_GATES) -> dict[str, str]:
    """Use only the newest completed/in-progress check of each exact gate name."""
    selected = {}
    for entry in checks:
        name = entry.get("name")
        if name not in required or entry.get("app", {}).get("slug") != "github-actions":
            continue
        sort_key = (entry.get("started_at") or "", entry.get("id") or 0)
        if name not in selected or sort_key > selected[name][0]:
            selected[name] = (sort_key, entry)
    return {
        name: ("MISSING" if name not in selected
               else "PENDING" if selected[name][1].get("status") != "completed"
               else str(selected[name][1].get("conclusion") or "UNKNOWN").upper())
        for name in required
    }


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
    while True:
        try:
            status = assess_checks(retrieve_checks(args.sha, token=os.getenv("GH_TOKEN", "")))
        except (HTTPError, URLError, OSError, ValueError) as error:
            print(f"GitHub gate lookup unavailable: {type(error).__name__}", file=sys.stderr)
            return 2
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
        if time.monotonic() >= deadline:
            print("Deployment candidate blocked: checks were not all successful", file=sys.stderr)
            return 1
        time.sleep(min(args.poll_seconds, max(0.1, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
