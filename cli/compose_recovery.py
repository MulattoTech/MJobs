"""Controlled outage/restart proof for this project's fictional Compose stack."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import httpx

from tests.integration.test_spine import PROFILE


def compose(*arguments):
    environment = os.environ.copy()
    environment.update(MJOBS_OPERATION_MODE="FIXTURE", MJOBS_COMPOSE_MOCK="1", COMPOSE_PROFILES="fixture")
    result = subprocess.run(["docker", "compose", "--profile", "fixture", *arguments], env=environment,
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError("Project Compose action failed: " + " ".join(arguments))
    return result.stdout


def run(baseline_path, evidence_path):
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    for key in ("application_id", "operation_id", "confirmation"):
        if key not in baseline:
            raise ValueError("Baseline is missing required recovery evidence: " + key)
    api = "http://127.0.0.1:18700"
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
            compose("stop", "worker", "source-worker", "document-worker", "redis")
            profile = dict(PROFILE, id="recovery-fictional-" + uuid4().hex[:12])
            request("PUT", "/profiles", json=profile)
            queued = request("POST", "/discovery", json={"profile_id": profile["id"]})
            assert queued["state"] == "QUEUED"
            degraded = client.get("/readiness")
            assert degraded.status_code == 503 and degraded.json()["checks"]["queue"] is False
            actions.append({"action": "Redis disconnected; discovery persisted", "result": "PASS", "operation_id": queued["operation_id"]})
            # --no-deps prevents Compose from helpfully restoring Redis early.
            compose("up", "-d", "--no-deps", "--no-build", "worker")
            until(lambda: int(subprocess.run(["docker", "inspect", "mjobs-worker-1", "--format", "{{.RestartCount}}"],
                capture_output=True, text=True, timeout=10, check=True).stdout.strip()) > 0)
            compose("start", "redis")
            compose("start", "worker", "source-worker", "document-worker")
            until(lambda: request("GET", "/operations/" + queued["operation_id"])["state"] == "COMPLETED")
            until(lambda: client.get("/readiness").status_code == 200)
            actions.append({"action": "Worker supervision and durable outbox recovered after Redis returned", "result": "PASS"})
            compose("restart", "postgres", "redis", "mock", "api", "worker", "source-worker", "document-worker")
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
            receipt = json.loads(compose("exec", "-T", "api", "python", "-c", program, baseline["application_id"]))
            assert receipt == baseline["confirmation"] and receipt["submission_count"] == 1
            actions.append({"action": "All services restarted; original application, operation and independent receipt survived without another submission", "result": "PASS"})
        finally:
            compose("start", "redis", "worker", "source-worker", "document-worker")
    evidence = {"result": "PASS", "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": "python -m cli.compose_recovery --baseline " + str(baseline_path),
        "target": "Project-only PostgreSQL/Redis/RQ/mock persistence and outage recovery", "actions": actions,
        "application_id": baseline["application_id"], "external_submission_enabled": False}
    Path(evidence_path).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    run(args.baseline, args.evidence)
