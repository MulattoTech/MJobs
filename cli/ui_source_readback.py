"""Verify stored real public jobs, manual preparation and visible source policy errors."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def classify_public_preparation(applications, alert_text):
    """Require a safe prepared application or the explicit missing-context block."""
    if applications:
        if len(applications) != 1 or applications[0].get("target") != "REAL" or applications[0].get("state") != "PREPARED":
            raise AssertionError("Public-job preparation created an unexpected application state")
        return {"status": "PREPARED", "application": applications[0], "reason": None}
    if "FAILED: COVER_LETTER_JOB_CONTEXT_MISSING" in str(alert_text):
        return {
            "status": "BLOCKED_MISSING_JOB_CONTEXT",
            "application": None,
            "reason": "The live posting did not provide a clear responsibility excerpt; no document or application was created.",
        }
    raise AssertionError("Public-job preparation neither completed safely nor reported the expected missing-context block")


def run(base_url, evidence_path):
    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(45000)
        page.goto(base_url, wait_until="networkidle")
        profiles = context.request.get(base_url + "/profiles").json()["profiles"]
        profile = next(row for row in profiles if row["id"].startswith("public-probe-fictional-"))
        page.get_by_label("Active profile").select_option(profile["id"])
        page.get_by_label("Source", exact=True).select_option("greenhouse")
        expect(page.locator(".job-list .job-card").first).to_be_visible()
        expect(page.locator(".job-list .job-card").first).to_contain_text("greenhouse · read only")
        page.locator(".job-list .job-card").first.click()
        expect(page.get_by_text("Weighted coverage of detected skill evidence; this is not a qualification assessment.", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="Open employer application")).to_be_visible()
        page.get_by_role("button", name="Prepare documents", exact=True).click()
        page.wait_for_function("""() => {
          const text = document.body.innerText;
          return text.includes('Application details') || text.includes('FAILED: COVER_LETTER_JOB_CONTEXT_MISSING');
        }""", timeout=45000)
        applications = context.request.get(base_url + "/applications?profile_id=" + profile["id"]).json()["applications"]
        alert_text = page.get_by_role("alert").inner_text() if page.get_by_role("alert").count() else ""
        preparation = classify_public_preparation(applications, alert_text)
        app = preparation["application"]
        if app:
            expect(page.get_by_role("heading", name="Application details")).to_be_visible()
            expect(page.get_by_text("Manual preparation only. Review the documents and use the employer’s own application page.", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Submit to local mock", exact=True)).to_have_count(0)
            rejected = context.request.post(base_url + "/applications/" + app["id"] + "/mock-run", data={"submit": True})
            assert rejected.status == 409
            actions = [{"action": "Public job prepared only as a manual-only real application; server rejects mock submission", "result": "PASS"}]
        else:
            expect(page.get_by_role("alert")).to_contain_text("FAILED: COVER_LETTER_JOB_CONTEXT_MISSING")
            assert applications == []
            actions = [{"action": "Live posting without a clear responsibility excerpt is blocked before document/application creation", "result": "PASS", "reason": preparation["reason"]}]
        # An ordinary second collection within cooldown executes zero provider requests.
        source = next(row for row in context.request.get(base_url + "/sources").json()["sources"] if row["tenant"] == "greenhouse")
        # Temporarily make the local cooldown test deterministic, preserving policy.
        policy = {
            "tenant": source["tenant"], "enabled": source["enabled"],
            "poll_interval": source["poll_interval"], "concurrency": source["concurrency"],
            "request_timeout": source["request_timeout"], "rate_limit": source["rate_limit"],
            "jitter_seconds": source["jitter_seconds"],
        }
        changed = context.request.put(base_url + "/sources", data={**policy, "poll_interval": 86400})
        assert changed.ok
        within_interval = (datetime.now(timezone.utc) - datetime.fromisoformat(source["last_attempt"])).total_seconds() < 86400
        actions.insert(0, {"action": "Stored live public jobs render heuristic match limitation and provider URL; preparation outcome is explicit and remains manual-only", "result": "PASS"})
        if within_interval:
            try:
                page.get_by_role("navigation").get_by_role("button", name="Sources").click()
                page.get_by_role("button", name="Collect now", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text("SOURCE_COOLDOWN")
                actions.append({"action": "Source cooldown is visible as a failed operation without repeating public requests", "result": "PASS"})
            finally:
                restored = context.request.put(base_url + "/sources", data=policy)
                assert restored.ok
        else:
            actions.append({"action": "Browser cooldown error after live collection", "result": "NOT_RUN", "reason": "Cooldown already elapsed; no redundant provider read was performed"})
        artifact = Path("outputs/ui-e2e/public-source-policy.png")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact), full_page=True)
        context.close()
        browser.close()
    evidence = {"result": "PASS", "timestamp": datetime.now(timezone.utc).isoformat(), "command": "python -m cli.ui_source_readback", "target": "Actual browser readback of persisted first-party public jobs",
        "application_id": app["id"] if app else None, "preparation_status": preparation["status"],
        "external_submission_enabled": False, "actions": actions, "artifact": str(artifact)}
    Path(evidence_path).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18700")
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    run(args.base_url, args.evidence)
