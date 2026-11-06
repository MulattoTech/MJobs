"""Real ATS adapter methods on static owned pages, without submit authority."""
import hashlib
from copy import deepcopy
from dataclasses import replace

import pytest
from playwright.sync_api import sync_playwright

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.adapters.lever import LeverApplicationAdapter
from services.applications.owned_ats_fixture import OwnedATSFixtureError, freeze_fixture
from tests.unit.test_owned_ats_fixture import fixture_inputs

HTML = """<!doctype html><html><body><form>
<input name="email" type="email" aria-label="Email" required>
<input name="resume" type="file" aria-label="Resume" required>
<input name="cover_letter" type="file" aria-label="Cover letter" required>
</form></body></html>"""


@pytest.fixture(params=["greenhouse", "lever"])
def owned_page(request):
    provider = request.param
    inputs = fixture_inputs(provider)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers="block")
            attempted_requests = []

            def owned_only(route):
                attempted_requests.append((route.request.method, route.request.url))
                if route.request.url == inputs["page_url"] and route.request.method == "GET":
                    route.fulfill(status=200, content_type="text/html", body=HTML,
                                  headers={"Content-Security-Policy": "default-src 'none'; form-action 'none'"})
                else:
                    route.abort()

            context.route("**/*", owned_only)
            page = context.new_page()
            page.goto(inputs["page_url"], wait_until="domcontentloaded")
            adapter = GreenhouseApplicationAdapter() if provider == "greenhouse" else LeverApplicationAdapter()
            inputs.update(template=page.content(), analysis=adapter.analyze_page(page))
            yield page, inputs, attempted_requests, adapter
            assert attempted_requests == [("GET", inputs["page_url"])]
        finally:
            browser.close()


def test_native_validation_blocks_upload_then_actual_adapter_selects_exact_bytes(owned_page):
    from services.applications.owned_ats_fixture import upload_fixture_documents
    page, inputs, _, adapter = owned_page
    contract = freeze_fixture(**inputs)
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_ADAPTER_REVIEW_REQUIRED"):
        upload_fixture_documents(page, contract, current=inputs)
    assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    result = upload_fixture_documents(page, contract, current=inputs)
    assert result["verification"] == "BROWSER_SELECTED_BYTES"
    assert result["external_submission_enabled"] is False
    assert result["documents"] == [document.manifest() for document in inputs["documents"]]
    assert page.locator("form").evaluate("form => form.checkValidity()") is True
    assert adapter.capabilities()["submission"] is False
    assert adapter.capabilities()["live_form_interaction"] is False


@pytest.mark.parametrize("changed", ["profile", "documents", "template", "question"])
def test_stale_review_prevents_any_document_selection(owned_page, changed):
    from services.applications.owned_ats_fixture import upload_fixture_documents
    page, inputs, _, _ = owned_page
    contract = freeze_fixture(**inputs)
    current = deepcopy(inputs)
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    if changed == "profile":
        current["profile"]["version"] += 1
    elif changed == "documents":
        doc = current["documents"][0]
        content = doc.content + b"changed"
        current["documents"][0] = replace(doc, content=content, sha256=hashlib.sha256(content).hexdigest())
    elif changed == "template":
        page.locator("form").evaluate("form => form.setAttribute('action', '/changed')")
    else:
        page.locator('[name="email"]').evaluate("node => node.setAttribute('aria-label', 'Another question')")
    with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_REVIEW_STALE"):
        upload_fixture_documents(page, contract, current=current)
    assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]


def test_reviewed_custom_binding_preserves_actual_provider_review_policy(owned_page):
    from services.applications.owned_ats_fixture import upload_fixture_documents
    page, inputs, _, adapter = owned_page
    page.locator("form").evaluate("""form => {
      const field = document.createElement('textarea');
      field.name = 'availability'; field.setAttribute('aria-label', 'Which days are you available?');
      form.appendChild(field);
    }""")
    inputs.update(template=page.content(), analysis=adapter.analyze_page(page),
                  reviewed_fields=({"field_key": "availability", "answer_id": "availability-1"},))
    contract = freeze_fixture(**inputs)
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    page.locator('[name="availability"]').fill("Weekdays only.")
    if inputs["provider"] == "lever":
        with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_ADAPTER_REVIEW_REQUIRED"):
            upload_fixture_documents(page, contract, current=inputs)
        assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]
    else:
        result = upload_fixture_documents(page, contract, current=inputs)
        assert len(result["documents"]) == 2


def test_extra_browser_page_stops_upload_before_selection(owned_page):
    from services.applications.owned_ats_fixture import upload_fixture_documents
    page, inputs, _, _ = owned_page
    contract = freeze_fixture(**inputs)
    page.locator('[name="email"]').fill(inputs["profile"]["email"])
    extra = page.context.new_page()
    try:
        with pytest.raises(OwnedATSFixtureError, match="OWNED_ATS_REVIEW_STALE"):
            upload_fixture_documents(page, contract, current=inputs)
        assert page.locator("input[type=file]").evaluate_all("nodes => nodes.map(n => n.files.length)") == [0, 0]
    finally:
        extra.close()
