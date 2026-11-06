"""Real Chromium checks for Mission Control campaign hierarchy rendering."""
from __future__ import annotations

import socket
import threading
import time
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import uvicorn

from tools.agent_dashboard.model_catalog import ModelCatalog, _choice
from tools.agent_dashboard.server import create_app

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]

def _visible_in_scrollport(locator, selector):
    return locator.evaluate("""(el, selector) => { const r=el.getBoundingClientRect();
      const p=el.closest(selector).getBoundingClientRect();
      return r.top >= p.top && r.bottom <= p.bottom && r.left >= p.left && r.right <= p.right; }""", selector)


def _run_browser(tmp_path, campaign_response, exercise):
    with patch.object(ModelCatalog, "_discover", return_value=(
        [_choice("gpt-6-luna", "Luna", ["high"], "high")], "fixture"
    )):
        app = create_app(ROOT, mission_instance=True, progress_history_db=tmp_path / "campaign.db")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 16
            while not server.started and time.monotonic() < deadline:
                time.sleep(.1)
            assert server.started, "Test server did not start"
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1500, "height": 1000})
                    page.set_default_timeout(18000)
                    calls = []
                    page.on("pageerror", lambda err: calls.append("pageerror:" + str(err)))
                    def serve_campaign(route):
                        sequence = 1 + calls.count("campaign")
                        calls.append("campaign")
                        result = campaign_response(sequence) if callable(campaign_response) else campaign_response
                        route.fulfill(status=200, json=result)
                    page.route("**/api/progress/campaigns", serve_campaign)
                    response = page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                    assert response.status == 200
                    exercise(page, calls, tmp_path)
                    assert not [x for x in calls if x.startswith("pageerror:")], calls
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def _campaign_fixture():
    statuses = ["PARTIAL"] * 39 + ["NOT_RUN"] * 21
    phases = []
    for phase_no, step_count in enumerate([8, 14, 9, 4, 18, 7], 1):
        steps = []
        offset = sum([8, 14, 9, 4, 18, 7][:phase_no - 1])
        for step_no in range(1, step_count + 1):
            number = offset + step_no
            steps.append({
                "number": number,
                "title": f"Campaign step {number}",
                "status": statuses[number - 1],
                "observed_result": "Synthetic local evidence recorded",
                "missing_behavior": "Independent qualification remains outstanding",
                "tested_commit": "abc123def456",
                "executed_at_utc": "2026-11-06T16:00:00Z",
                "review_status": "COMMENTED",
                "merge_status": "NOT_MERGED",
            })
        label = chr(64 + phase_no)
        phase_name = [
            "Intake and scope (Steps 1–8)", "Source coverage (Steps 9–22)",
            "Collection (Steps 23–31)", "Normalization (Steps 32–35)",
            "Qualification (Steps 36–53)", "Closeout (Steps 54–60)",
        ][phase_no - 1]
        phases.append({"id": f"phase-{label}", "label": phase_name, "steps": steps})
    return {
        "schema_version": 1,
        "available": True,
        "requirements": [{"id": "MJ-REQ-292", "title": "Catalog title for reliable application workflow"}],
        "observed_at": "2026-11-06T16:00:00Z",
        "campaigns": [{
            "requirement_id": "MJ-REQ-292",
            "title": "Reliable employment application workflow",
            "issue_url": "https://github.com/MulattoTech/MJobs/issues/293",
            "updated_at_utc": "2026-11-06T16:00:00Z",
            "source_revision": "abc123def456",
            "source_kind": "WORKING_CHECKPOINT",
            "handoff_revision": "70595258e4cac188d641629298a5ee4e8131a45c",
            "routing_state": "PAUSED",
            "routing_state_scope": "Campaign handoff state",
            "acceptance_claim_scope": "Recorded ledger claims; independent runtime and merge certification remain separate.",
            "scoreboard": {"total": 60, "verified_pass": 0, "PARTIAL": 39, "NOT_RUN": 21, "FAIL": 0, "BLOCKED": 0},
            "phases": phases,
            "blockers": ["External validation remains pending", "Independent merge certification remains pending"],
            "next_action": "Continue the local qualification campaign.",
        }],
    }


def test_p0_campaign_hierarchy_is_readable_compact_and_survives_refresh(tmp_path):
    response = _campaign_fixture()

    def exercise(page, calls, tmp_path):
        page.locator("#releaseContent .release-summary").wait_for(state="attached")
        # Overall acceptance remains the first content, then the MJ-REQ-292 row
        # appears within P0 with its readable title and an explicit disclosure.
        assert page.locator("#releaseContent .release-summary").first.is_visible()
        row = page.locator('.progress-group-item[data-key="priorities:P0"]')
        row.wait_for(state="attached")
        row.locator(":scope > summary").click()
        requirement = row.locator(".progress-drilldown .progress-item").filter(has_text="MJ-REQ-292")
        assert row.locator(".progress-drilldown .progress-item").first.locator("code").inner_text() == "MJ-REQ-292"
        assert requirement.locator(".progress-item-identity strong").inner_text() == "Catalog title for reliable application workflow"
        assert requirement.locator(".campaign-row-score").inner_text() == "0 verified · 39 partial · 21 not run"
        assert _visible_in_scrollport(requirement, ".progress-item-list")
        campaign = requirement.locator("details.campaign-details")
        campaign.wait_for(state="attached")
        campaign.locator(":scope > summary").click()
        assert campaign.inner_text().find("0 verified · 39 partial · 21 not run") >= 0
        assert campaign.inner_text().find("Recorded checkpoint · updated") >= 0
        assert campaign.inner_text().find("abc123def456") == -1
        assert campaign.inner_text().find("70595258e4cac188d641629298a5ee4e8131a45c") == -1
        assert campaign.inner_text().find("per-step merge state is shown below") >= 0
        assert campaign.get_by_text("Blocker: External validation remains pending", exact=True).is_visible()
        assert campaign.get_by_text("Next action: Continue the local qualification campaign.", exact=True).is_visible()
        page.locator("#releaseTracker").screenshot(path=str(tmp_path / "campaign-drilldown-preview.png"))
        provenance = campaign.locator("details.campaign-source")
        assert provenance.locator(":scope > summary").inner_text() == "Evidence and source"
        provenance.locator(":scope > summary").click()
        assert provenance.inner_text().find("abc123def456") >= 0
        assert provenance.inner_text().find("70595258e4cac188d641629298a5ee4e8131a45c") >= 0
        assert provenance.inner_text().find("Last recorded checkpoint state: PAUSED") >= 0
        assert provenance.inner_text().find("Recorded ledger claims") >= 0
        blockers = campaign.locator("details.campaign-blockers")
        assert blockers.locator(":scope > summary").inner_text() == "All recorded blockers (2)"
        blockers.locator(":scope > summary").click()
        assert blockers.get_by_text("Independent merge certification remains pending", exact=True).is_visible()
        phases = campaign.locator("details.campaign-phase")
        assert phases.count() == 6
        assert [phases.nth(i).locator(":scope > summary .campaign-phase-label").inner_text() for i in range(6)] == [
            "Intake and scope (Steps 1–8)", "Source coverage (Steps 9–22)", "Collection (Steps 23–31)",
            "Normalization (Steps 32–35)", "Qualification (Steps 36–53)", "Closeout (Steps 54–60)",
        ]
        assert "0 fail" not in phases.first.locator(":scope > summary").inner_text()
        assert "0 blocked" not in phases.first.locator(":scope > summary").inner_text()
        assert sum(phases.nth(i).locator("details.campaign-step").count() for i in range(6)) == 60
        assert phases.first.locator("details.campaign-step > summary").first.is_visible() is False
        phase = phases.first
        phase.locator(":scope > summary").click()
        step = phase.locator("details.campaign-step").first
        step.locator(":scope > summary").click()
        assert step.inner_text().find("Tested commit") >= 0
        assert step.inner_text().find("Remaining evidence gap") >= 0
        assert step.inner_text().find("NOT_MERGED") >= 0
        # The five-second live progress refresh fetches new campaign data without
        # losing the open requirement, campaign, phase, or step disclosures.
        page.wait_for_function("() => performance.getEntriesByType('resource').filter(x => x.name.includes('/api/progress/campaigns')).length >= 2", timeout=16000)
        page.wait_for_timeout(150)
        assert row.get_attribute("open") is not None
        assert campaign.get_attribute("open") is not None
        assert provenance.get_attribute("open") is not None
        assert blockers.get_attribute("open") is not None
        assert phase.get_attribute("open") is not None
        assert step.get_attribute("open") is not None
        assert requirement.inner_text().find("Catalog title for reliable application workflow") >= 0
        assert requirement.is_visible()
        assert phase.locator(":scope > summary").is_visible()
        assert campaign.evaluate("el => getComputedStyle(el.closest('.progress-item-list')).maxHeight") == "none"

    _run_browser(tmp_path, response, exercise)


def test_campaign_unavailable_does_not_render_zero_totals(tmp_path):
    def exercise(page, calls, tmp_path):
        row = page.locator('.progress-group-item[data-key="priorities:P0"]')
        row.wait_for(state="attached")
        row.locator(":scope > summary").click()
        body = row.locator(".progress-drilldown .progress-item").filter(has_text="MJ-REQ-292")
        assert body.get_by_text("Campaign details unavailable", exact=False).count() == 1
        assert body.get_by_text("0 verified", exact=False).count() == 0

    _run_browser(tmp_path, {"schema_version": 1, "available": False, "campaigns": []}, exercise)


def test_campaign_success_then_failure_clears_snapshot_but_keeps_catalog_title(tmp_path):
    valid = _campaign_fixture()

    def response(sequence):
        return valid if sequence == 1 else {
            "schema_version": 1, "available": False,
            "requirements": valid["requirements"], "campaigns": [],
            "error": "Campaign checkpoint is unavailable or inconsistent.",
        }

    def exercise(page, calls, tmp_path):
        row = page.locator('.progress-group-item[data-key="priorities:P0"]')
        row.locator(":scope > summary").click()
        requirement = row.locator(".progress-drilldown .progress-item").first
        campaign = requirement.locator("details.campaign-details")
        campaign.locator(":scope > summary").click()
        assert campaign.get_by_text("39 partial", exact=False).count() == 1
        page.wait_for_function("() => performance.getEntriesByType('resource').filter(x => x.name.includes('/api/progress/campaigns')).length >= 2", timeout=16000)
        page.wait_for_function("() => document.querySelector('.campaign-unavailable')?.textContent.includes('unavailable or inconsistent')")
        assert requirement.locator(".progress-item-identity strong").inner_text() == "Catalog title for reliable application workflow"
        assert requirement.locator(".campaign-row-score").count() == 0
        assert campaign.get_by_text("39 partial", exact=False).count() == 0
        assert campaign.locator(".campaign-phase").count() == 0

    _run_browser(tmp_path, response, exercise)
