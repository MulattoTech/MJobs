"""Verify personal operation in an isolated, empty Compose project."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import httpx
from playwright.sync_api import sync_playwright


def run(api_url: str, evidence_path: Path | None = None) -> dict:
    with httpx.Client(base_url=api_url, timeout=15, trust_env=False) as client:
        deadline = time.monotonic() + 90
        while True:
            response = client.get("/readiness")
            if response.status_code == 200:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Personal-mode services did not become ready")
            time.sleep(1)
        settings = client.get("/settings").json()
        health = client.get("/health").json()
        if settings.get("operation_mode") != "PERSONAL" or health.get("operation_mode") != "PERSONAL":
            raise RuntimeError("API did not enter personal operation mode")
        if settings.get("external_submission_enabled") is not False or settings.get("browser_workers") != 0:
            raise RuntimeError("Personal operation exposed an application automation switch")
        if "mock" in response.json().get("checks", {}):
            raise RuntimeError("Personal readiness still depends on fictional mock service")
        empty_surfaces = {
            "profiles": client.get("/profiles").json()["profiles"],
            "jobs": client.get("/jobs").json()["jobs"],
            "applications": client.get("/applications").json()["applications"],
            "sources": client.get("/sources").json()["sources"],
        }
        if any(empty_surfaces.values()):
            raise RuntimeError("Personal project inherited fixture or other prior data")
        rejected = {
            "fictional_discovery": client.post("/discovery", json={"profile_id": "isolated-probe"}).status_code,
            "fictional_submission": client.post("/applications/isolated-probe/mock-run", json={"submit": True}).status_code,
        }
        if any(status != 409 for status in rejected.values()):
            raise RuntimeError("A fictional action was available in personal mode: " + repr(rejected))
        profile_id = "personal-upload-probe"
        profile = {"id": profile_id, "name": "Synthetic Upload Probe", "email": "upload@example.invalid",
                   "facts": [{"id": "synthetic", "kind": "skill", "text": "Synthetic test input",
                              "source": "Isolated CI test", "verified": False, "user_approved": False,
                              "confidence": 0}], "answers": []}
        client.put("/profiles", json=profile).raise_for_status()
    sample = b"%PDF-1.4\n%%EOF\n"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(api_url, wait_until="domcontentloaded", timeout=20000)
            page.locator('[data-operation-mode="PERSONAL"]').wait_for(timeout=20000)
            if page.get_by_role("button", name="Load fictional demo").count():
                raise RuntimeError("Personal UI still offers the fictional demo")
            page.get_by_role("button", name="Sources", exact=True).click()
            page.get_by_role("heading", name="Public Greenhouse board").wait_for(timeout=10000)
            if page.get_by_role("heading", name="Fictional local source").count():
                raise RuntimeError("Personal UI still offers fictional source actions")
            page.locator("#active-profile").select_option(profile_id)
            page.get_by_role("button", name="Profile", exact=True).click()
            page.get_by_label("Choose a resume (up to 10 MiB)").set_input_files({
                "name": "Synthetic Resume.pdf", "mimeType": "application/pdf", "buffer": sample})
            page.get_by_role("button", name="Store resume").click()
            page.get_by_text("STORED UNREVIEWED").wait_for(timeout=15000)
        finally:
            browser.close()
    with httpx.Client(base_url=api_url, timeout=15, trust_env=False) as client:
        stored = client.get(f"/profiles/{profile_id}/resumes")
        stored.raise_for_status()
        resumes = stored.json()["resumes"]
        if len(resumes) != 1 or resumes[0]["status"] != "STORED_UNREVIEWED":
            raise RuntimeError("Browser upload did not create one unreviewed resume")
        downloaded = client.get(f"/profiles/{profile_id}/resumes/{resumes[0]['id']}")
        downloaded.raise_for_status()
        if downloaded.content != sample or resumes[0]["sha256"] != hashlib.sha256(sample).hexdigest():
            raise RuntimeError("Stored resume failed API download/checksum readback")
        facts = client.get(f"/profiles/{profile_id}").json()["facts"]
        if len(facts) != 1 or facts[0]["verified"] or facts[0]["user_approved"]:
            raise RuntimeError("Resume upload silently approved a career fact")
    evidence = {
        "result": "PASS",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target": "Isolated personal Compose project, API and connected Chromium UI",
        "command": "python -m cli.personal_mode_e2e --api " + api_url,
        "operation_mode": "PERSONAL",
        "rejected_fixture_actions": rejected,
        "empty_personal_surfaces": list(empty_surfaces),
        "public_source_ui_available": True,
        "fictional_ui_actions_hidden": True,
        "uploaded_resume": {"status": "STORED_UNREVIEWED", "format": "pdf", "bytes_verified": len(sample),
                            "browser_path": "Profile file input and Store resume", "facts_approved": 0},
        "external_submission_enabled": False,
    }
    if evidence_path:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:18700")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    run(args.api, args.evidence)
