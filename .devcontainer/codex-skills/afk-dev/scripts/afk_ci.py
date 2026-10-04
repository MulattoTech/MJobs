"""Create exact-check GitHub Actions failure packets without mixed-run log tails."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from afk_redact import redact, sanitize

DETAILS = re.compile(r"/actions/runs/(?P<run>\d+)/job/(?P<job>\d+)")


def packet(check, job, log, *, repository, pr, head):
    match = DETAILS.search(check.get("details_url", ""))
    if not match:
        raise ValueError(f"Check {check.get('name')} has no exact Actions job mapping")
    run_id, job_id = int(match["run"]), int(match["job"])
    if job.get("id") != job_id or job.get("run_id") != run_id:
        raise ValueError("Check/job identity mismatch")
    if check.get("head_sha") and check["head_sha"] != head:
        raise ValueError("Stale check for a different head")
    if job.get("head_sha") and job["head_sha"] != head:
        raise ValueError("Stale job for a different head")
    lines = redact(log).splitlines()
    return sanitize({"repository": repository, "pr": pr, "headSha": head, "checkName": check["name"], "workflow": job.get("workflow_name"), "runId": run_id, "jobId": job_id, "attempt": job.get("run_attempt"), "conclusion": check.get("conclusion"), "detailsUrl": check["details_url"], "logSource": f"gh run view {run_id} --job {job_id} --log", "logTail": lines[-80:]})


def packets_from_fixture(data, selected=None):
    checks = data["checks"]
    jobs = {int(j["id"]): j for j in data["jobs"]}
    logs = data["jobLogs"]
    result = []
    if selected and not any(check.get("name") == selected for check in checks):
        raise ValueError(f"Selected check not found: {selected}")
    for check in checks:
        if selected and check["name"] != selected:
            continue
        if check.get("conclusion") not in {"failure", "timed_out", "cancelled", "action_required"}:
            continue
        match = DETAILS.search(check.get("details_url", ""))
        if not match:
            raise ValueError(f"Selected failing check has no exact Actions job mapping: {check['name']}")
        job_id = int(match["job"])
        result.append(packet(check, jobs[job_id], logs[str(job_id)], repository=data["repository"], pr=data["pr"], head=data["headSha"]))
    return result


def gh_json(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(redact(result.stderr[-300:]))
    return json.loads(result.stdout)


def live_packets(pr=None, selected=None):
    repo = gh_json("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    pr_args = ["pr", "view"] + ([str(pr)] if pr else []) + ["--json", "number,headRefOid"]
    pr_data = gh_json(*pr_args)
    head = pr_data["headRefOid"]
    pages = gh_json("api", "--paginate", "--slurp", f"repos/{repo}/commits/{head}/check-runs?per_page=100")
    checks = [check for page in pages for check in page.get("check_runs", [])]
    if selected and not any(check.get("name") == selected for check in checks):
        raise ValueError(f"Selected check not found on current head {head}: {selected}")
    result = []
    for check in checks:
        if selected and check["name"] != selected:
            continue
        if check.get("conclusion") not in {"failure", "timed_out", "cancelled", "action_required"}:
            continue
        match = DETAILS.search(check.get("details_url", ""))
        if not match:
            raise ValueError(f"Failing check has no exact Actions job mapping: {check['name']} at {check.get('details_url')}")
        job_id, run_id = int(match["job"]), int(match["run"])
        job = gh_json("api", f"repos/{repo}/actions/jobs/{job_id}")
        log_result = subprocess.run(["gh", "run", "view", str(run_id), "--job", str(job_id), "--log"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if log_result.returncode:
            raise RuntimeError(f"Job log unavailable for {job_id}: {redact(log_result.stderr[-300:])}")
        result.append(packet(check, job, log_result.stdout, repository=repo, pr=pr_data["number"], head=head))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--pr")
    parser.add_argument("--check")
    args = parser.parse_args(argv)
    result = packets_from_fixture(json.loads(args.fixture.read_text(encoding="utf-8")), args.check) if args.fixture else live_packets(args.pr, args.check)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
