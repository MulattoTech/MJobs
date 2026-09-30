"""Bounded Compose API/worker proof using explicit fictional profile data."""
import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx

from tests.integration.test_spine import PROFILE


def run(api_url, evidence_path=None):
    with httpx.Client(base_url=api_url, timeout=15, trust_env=False) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        def wait(operation_id):
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                operation = call("GET", "/operations/" + operation_id)
                if operation["state"] in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
                    if operation["state"] != "COMPLETED":
                        raise RuntimeError("Operation failed: " + str(operation["error"]))
                    return operation
                time.sleep(0.5)
            raise TimeoutError("Mock operation exceeded bounded completion window")

        health = call("GET", "/health")
        if health.get("external_submission_enabled") is not False or health.get("mode") not in ("MOCK", "LOCAL_FIRST"):
            raise RuntimeError("Expected local mode and external submission OFF")
        profile = dict(PROFILE, id="e2e-fictional-" + uuid4().hex[:12])
        call("PUT", "/profiles", json=profile)
        discovery = call("POST", "/discovery", json={"profile_id": profile["id"]})
        wait(discovery["operation_id"])
        jobs = call("GET", "/jobs", params={"profile_id": profile["id"]})["jobs"]
        job_b = next(job for job in jobs if job["requisition"] == "B")
        preparation = call("POST", "/applications", json={"job_id": job_b["id"], "profile_id": profile["id"]})
        wait(preparation["operation_id"])
        application = call("GET", "/applications/" + preparation["application_id"])
        request = call("POST", "/applications/" + application["id"] + "/mock-run", json={"submit": True})
        wait(request["operation_id"])
        confirmed = call("GET", "/applications/" + application["id"])
        if confirmed["state"] != "CONFIRMED" or not confirmed["confirmation"]:
            raise RuntimeError("No verified fictional mock submission confirmation")
        # Repeat the exact operation and assert no new operation/application.
        again = call("POST", "/applications/" + application["id"] + "/mock-run", json={"submit": True})
        if again["operation_id"] != request["operation_id"]:
            raise RuntimeError("Operation identity changed on retry")
        wait(again["operation_id"])
        if call("GET", "/applications/" + application["id"])["confirmation"]["submission_count"] != 1:
            raise RuntimeError("Duplicate fictional submission detected")
        evidence = {"result": "PASS", "target": "PostgreSQL/Redis/RQ controlled mock application spine",
            "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "command": "python -m cli.mock_e2e --api " + api_url, "application_id": application["id"],
            "operation_id": request["operation_id"], "profile_id": profile["id"], "canonical_jobs": len(jobs),
            "confirmation": confirmed["confirmation"], "trace": confirmed["trace"], "external_submission_enabled": False}
        if evidence_path:
            target = Path(evidence_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, indent=2))
        return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:18700")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    run(args.api, args.evidence)
