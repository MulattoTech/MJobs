"""Browser smoke tests for the offline Feature Studio intake GUI."""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from tools.feature_studio.validate import STUDIO, ROOT, validate_spec

HTML = ROOT / "tools" / "agent_dashboard" / "static" / "feature-studio.html"
SCHEMA = json.loads((STUDIO / "schema.v1.json").read_text(encoding="utf-8"))


def test_feature_studio_export_and_adaptive_probing():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(HTML.resolve().as_uri())
            assert page.get_by_role("heading", name="Guided feature intake").is_visible()
            page.locator("#scope").select_option("ai")
            assert "model providers" in page.locator("#probes").inner_text().lower()
            page.locator("#scope").select_option("full")
            page.locator("#risk").select_option("low")
            with page.expect_download() as download_info:
                page.get_by_role("button", name="Download JSON specification").click()
            download = download_info.value
            payload = json.loads(Path(download.path()).read_text(encoding="utf-8"))
            assert download.suggested_filename == "FS-TEST-001.json"
            assert validate_spec(payload, SCHEMA) == []
            assert payload["lifecycle"] == "DRAFT"
            assert payload["execution"]["agent_execution_authorized"] is False
            assert all(item["status"] == "NOT_RUN" for item in payload["acceptance_criteria"])
        finally:
            browser.close()


def test_feature_studio_import_strips_approval_claims(tmp_path):
    record = json.loads((STUDIO / "specs" / "FS-TEST-001.json").read_text(encoding="utf-8"))
    record["lifecycle"] = "VERIFIED"
    record["execution"]["agent_execution_authorized"] = True
    record["acceptance_criteria"][0]["status"] = "PASS"
    record["acceptance_criteria"][0]["evidence"] = ["fabricated"]
    file_path = tmp_path / "untrusted.json"
    file_path.write_text(json.dumps(record), encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(HTML.resolve().as_uri())
            page.locator("#file").set_input_files(str(file_path))
            preview = json.loads(page.locator("#preview").inner_text())
            assert preview["lifecycle"] == "DRAFT"
            assert not preview["execution"]["agent_execution_authorized"]
            assert all(item["status"] == "NOT_RUN" for item in preview["acceptance_criteria"])
            assert "reset" in page.locator("#status").inner_text()
        finally:
            browser.close()
