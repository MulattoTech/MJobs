"""Playwright verifies the ledger renders and paginates recorded metadata safely."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "tools" / "agent_dashboard" / "static" / "index.html"
SCRIPT = ROOT / "tools" / "agent_dashboard" / "static" / "delivery_ledger.js"
STYLE = ROOT / "tools" / "agent_dashboard" / "static" / "delivery_ledger.css"


def fixture_data(kind: str, offset: int, limit: int, search: str):
    records = [
        {"id": "workspace:work", "kind": "workspace",
         "at": "2026-10-14T18:00:00Z", "title": "2 uncommitted files",
         "source": "Local working-copy observation", "workspace": "C:\\MJobs",
         "changed_count": 2, "paths": ["main.py", "test_main.py"]},
        {"id": "matrix:1:MJ-REQ-277", "kind": "acceptance",
         "at": "2026-10-14T17:00:00Z",
         "title": "MJ-REQ-277: PARTIAL -> PASS",
         "source": "Git main acceptance matrix",
         "requirement_id": "MJ-REQ-277", "before": "PARTIAL",
         "after": "PASS", "verified_pass": True,
         "url": "https://github.com/MulattoTech/MJobs/issues/278"},
        {"id": "commit:abc", "kind": "commit",
         "at": "2026-10-14T16:00:00Z",
         "title": "<img src=x onerror=window.__ledgerHacked=1>",
         "source": "Git history", "sha": "a" * 40, "scopes": ["local main"]},
        {"id": "pr:merged:639", "kind": "pr",
         "at": "2026-10-14T15:00:00Z", "title": "PR #639 merged: tooling",
         "source": "GitHub pull requests", "number": 639,
         "url": "https://github.com/MulattoTech/MJobs/pull/639"},
    ]
    selected = records
    if kind == "pass":
        selected = [r for r in records if r.get("verified_pass")]
    elif kind != "all":
        selected = [r for r in records if r["kind"] == kind]
    if search:
        selected = [r for r in selected if search.casefold() in r["title"].casefold()
                    or search.casefold() in str(r.get("requirement_id", "")).casefold()]
    return {
        "available": True, "events": selected[offset:offset + limit],
        "total_recorded": len(records), "total_matching": len(selected),
        "offset": offset, "limit": limit,
        "has_more": offset + limit < len(selected), "kind": kind,
        "search": search, "observed_at": "2026-10-14T18:30:00Z",
        "counts": {"workspace": 1, "acceptance": 1, "commit": 1, "pr": 1},
        "coverage": {
            "git": {"unique_commits": 1, "scopes": {"canonical": 1}},
            "matrix": {"original": {"revisions_examined": 2},
                       "reference": {"revisions_examined": 1}},
            "workspaces": {"registered_and_independent": 2,
                           "statuses_inspected": 2},
            "github": {
                "pull_requests": {"available": True, "records": 1},
                "issues": {"available": True, "records": 2},
                "branches": {"available": True, "records": 1, "heads": [
                    {"name": "main", "sha": "a" * 40}]}},
        },
        "workspaces": [
            {"workspace": "C:\\MJobs", "branch": "main",
             "kind": "registered", "state": "DIRTY", "changed_count": 2},
            {"workspace": "C:\\MJobs-archive", "branch": "feature",
             "kind": "independent clone", "state": "CLEAN"},
        ],
        "note": "No fake completion metrics, no prompts executed.",
    }


def test_browser_delivery_timeline_and_search():
    source = HTML.read_text(encoding="utf8")
    section = re.search(r'<section class="panel ledger-panel".*?</section>', source, flags=re.S)
    assert section is not None
    assert SCRIPT.is_file() and STYLE.is_file()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.route("http://mjobs.local/**", lambda route: route.fulfill(
            content_type="text/html", body="<html><body>" + section.group(0) + "</body></html>"
        ) if urlsplit(route.request.url).path == "/" else route.continue_())

        def ledger(route):
            q = parse_qs(urlsplit(route.request.url).query)
            kind = q.get("kind", ["all"])[0]
            offset = int(q.get("offset", ["0"])[0])
            limit = int(q.get("limit", ["60"])[0])
            search = q.get("search", [""])[0]
            route.fulfill(content_type="application/json",
                          body=json.dumps(fixture_data(kind, offset, limit, search)))
        page.route("**/api/delivery-ledger?*", ledger)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://mjobs.local/", wait_until="domcontentloaded")
        page.add_script_tag(path=str(SCRIPT))
        page.locator(".ledger-record").first.wait_for(timeout=8000)
        assert page.locator(".ledger-record").count() == 4
        page.wait_for_function("document.querySelector('#ledgerStatus').textContent.startsWith('Observed')")
        assert page.locator("#ledgerHighlights").inner_text().find("MJ-REQ-277") >= 0
        page.locator("#ledgerCoverage summary").click()
        assert page.locator("#ledgerCoverage").inner_text().find("GitHub PR records") >= 0
        assert page.locator("#ledgerWorkspaceSummary").inner_text().find("(2)") >= 0
        assert page.evaluate("window.__ledgerHacked === undefined")
        assert page.locator(".ledger-record.commit img").count() == 0
        page.locator("#ledgerKind").select_option("pass")
        page.locator(".ledger-record").first.wait_for(timeout=8000)
        page.wait_for_function(
            "document.querySelector('#ledgerPaging').textContent.includes('1 of 1')"
        )
        assert page.locator(".ledger-record").count() == 1
        assert "MJ-REQ-277" in page.locator(".ledger-record").first.inner_text()
        page.locator("#ledgerKind").select_option("all")
        page.wait_for_function(
            "document.querySelector('#ledgerPaging').textContent.includes('4 of 4')"
        )
        page.locator("#ledgerSearch").fill("MJ-REQ-277")
        page.locator("#ledgerSearchGo").click()
        page.wait_for_function(
            "document.querySelector('#ledgerPaging').textContent.includes('1 of 1')"
        )
        assert page.locator(".ledger-record").count() == 1
        page.locator("#ledgerSearch").fill("")
        page.locator("#ledgerSearchGo").click()
        page.wait_for_function(
            "document.querySelector('#ledgerPaging').textContent.includes('4 of 4')"
        )
        with page.expect_download(timeout=10000) as download_info:
            page.locator("#ledgerExport").click()
        download = download_info.value
        assert download.suggested_filename == "MJobs-recorded-delivery-history.json"
        assert not errors, errors
        browser.close()
