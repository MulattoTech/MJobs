"""Opt-in live, public GET proof through the deployed source worker; no submit."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx

from tests.integration.test_spine import PROFILE


def run(base_url, tenant, evidence_path):
    with httpx.Client(base_url=base_url, timeout=15, trust_env=False) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        profile = dict(PROFILE, id="public-probe-fictional-" + uuid4().hex[:12])
        request("PUT", "/profiles", json=profile)
        source_id = request("PUT", "/sources", json={"tenant": tenant})["id"]
        queued = request("POST", "/sources/" + source_id + "/collect", json={"profile_id": profile["id"], "request_id": uuid4().hex})
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            operation = request("GET", "/operations/" + queued["operation_id"])
            if operation["state"] in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
                break
            time.sleep(.5)
        else:
            raise TimeoutError("Live public collection did not finish within 120 seconds")
        source = next(row for row in request("GET", "/sources")["sources"] if row["id"] == source_id)
        jobs = [job for job in request("GET", "/jobs", params={"profile_id": profile["id"], "provider": "greenhouse", "limit": 500})["jobs"] if job["attributes"].get("source_id") == source_id]
        result = "PASS" if operation["state"] == "COMPLETED" and jobs and source["health_status"] in ("HEALTHY", "PARTIAL") else "FAIL"
        examples = []
        if result == "PASS":
            for job in jobs[:3]:
                detail = request("GET", "/jobs/" + job["id"], params={"profile_id": profile["id"]})
                assert detail["postings"] and detail["postings"][0]["snapshots"]
                assert detail["match"]["components"]["method"] == "description_skill_mentions"
                examples.append({"job_id": job["id"], "title": job["title"], "company": job["company"], "native_posting_id": job["attributes"]["native_posting_id"],
                    "source_endpoint": job["attributes"]["source_endpoint"], "sha256": detail["postings"][0]["snapshots"][0]["sha256"], "submission_capability": job["attributes"]["submission_capability"]})
        assert request("GET", "/health")["external_submission_enabled"] is False
        evidence = {"result": result, "timestamp": datetime.now(timezone.utc).isoformat(), "command": "python -m cli.public_source_probe --tenant " + tenant,
            "target": "Actual public Greenhouse GET / deployed RQ source worker / PostgreSQL provenance", "source_id": source_id,
            "profile_id": profile["id"], "operation_id": queued["operation_id"], "operation_state": operation["state"], "error_category": operation["error"], "source": source,
            "job_count_returned": len(jobs), "examples": examples, "external_submission_enabled": False, "external_application_requests": 0}
        Path(evidence_path).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, indent=2))
        if result != "PASS":
            raise RuntimeError("Live public source verification failed: " + str(operation["error"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18700")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    run(args.base_url, args.tenant, args.evidence)
