"""Real Chromium journeys through the explicitly fictional local fixture."""
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import pytest

from fixtures.mock_server import fixture_server


PDF = b"%PDF-1.4\n% fictional browser upload\n%%EOF\n"
ANSWER = "Controlled fictional test application."


@pytest.fixture
def local_browser(tmp_path):
    with fixture_server(tmp_path / "receipts.json", scenario="coverage") as origin:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            external = []

            def local_only(route):
                if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                    external.append(route.request.url)
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", local_only)
            page = context.new_page()
            page.set_default_timeout(5000)
            try:
                yield page, origin
                assert not external
            finally:
                context.close()
                browser.close()


def fill_contact_and_uploads(page):
    page.get_by_label("Name", exact=True).fill("Alex Fixture")
    page.get_by_label("Email", exact=True).fill("alex@example.invalid")
    for label, name in [("Resume", "resume.pdf"), ("Cover letter", "cover.pdf")]:
        page.get_by_label(label, exact=True).set_input_files({
            "name": name, "mimeType": "application/pdf", "buffer": PDF,
        })
    page.locator('input[type="radio"][name="remote"]').check()
    page.get_by_label("Documents reviewed", exact=True).check()


def fill_confirmation(page, identity):
    page.get_by_label("Application ID", exact=True).fill(identity)
    page.get_by_label("Approved fixture answer", exact=True).fill(ANSWER)
    page.get_by_label("Confirm fictional test submission", exact=True).check()


def test_named_company_careers_redirect_and_jobs_are_navigable(local_browser):
    page, origin = local_browser
    redirects = []
    page.on("response", lambda response: redirects.append((response.url, response.status))
            if response.url == origin + "/company/careers" else None)
    page.goto(origin + "/company")
    expect(page.get_by_role("heading", name="Example Systems Inc.", exact=True)).to_be_visible()
    page.get_by_role("link", name="Careers", exact=True).click()
    expect(page).to_have_url(origin + "/ats/example")
    assert redirects and redirects[0][1] in {302, 303, 307, 308}
    with page.expect_response(origin + "/ats/example/jobs") as requested:
        page.get_by_role("link", name="Jobs", exact=True).click()
    rows = requested.value.json()["jobs"]
    assert {row["requisition"] for row in rows} == {"A", "B"}
    assert {row["company"] for row in rows} == {"Example Systems Inc."}
    assert len({row["application_url"] for row in rows}) == 2
    assert page.request.get(origin + "/_test/request-counts").json() == {"review": 0, "submit": 0}


def test_onepage_upload_controls_conditional_lifecycle_and_confirmation(local_browser):
    page, origin = local_browser
    page.goto(origin + "/ats/apply/A?flow=onepage&dynamic=known")
    region = page.get_by_label("State or province", exact=True)
    expect(region).to_be_hidden()
    fill_contact_and_uploads(page)
    country = page.get_by_label("Country", exact=True)
    country.select_option(label="United States")
    expect(region).to_be_visible()
    assert region.get_attribute("required") is not None
    region.select_option(label="Oklahoma")
    country.select_option(label="Canada")
    expect(region).to_be_hidden()
    assert region.get_attribute("required") is None
    assert region.input_value() == ""
    country.select_option(label="United States")
    expect(region).to_be_visible()
    assert region.get_attribute("required") is not None
    fill_confirmation(page, "browser-onepage")
    page.get_by_role("button", name="Submit mock application", exact=True).click()
    assert page.request.get(origin + "/_test/request-counts").json()["submit"] == 0
    region.select_option(label="Oklahoma")
    with page.expect_response(lambda response: "/ats/onepage/A" in response.url
                              and response.request.method == "POST") as submitted:
        page.get_by_role("button", name="Submit mock application", exact=True).click()
    assert submitted.value.status == 200
    expect(page.get_by_role("heading", name="Thank you for applying.", exact=True)).to_be_visible()
    receipt = page.request.get(origin + "/ats/receipts/browser-onepage").json()
    assert receipt["submission_count"] == 1
    assert receipt["requisition"] == "A"
    assert page.request.get(origin + "/_test/request-counts").json() == {"review": 0, "submit": 1}


def test_multipage_validation_error_then_review_question_and_confirmation(local_browser):
    page, origin = local_browser
    page.goto(origin + "/ats/apply/B")
    fill_contact_and_uploads(page)
    page.get_by_label("Country", exact=True).select_option(label="United States")
    page.get_by_label("Resume", exact=True).set_input_files({
        "name": "resume.pdf", "mimeType": "application/pdf", "buffer": b"invalid document",
    })
    with page.expect_response(lambda response: "/ats/review/B" in response.url
                              and response.request.method == "POST") as rejected:
        page.get_by_role("button", name="Review application", exact=True).click()
    assert rejected.value.status == 422
    expect(page.get_by_role("alert")).to_contain_text("Invalid document")
    assert page.request.get(origin + "/_test/request-counts").json()["submit"] == 0
    page.goto(origin + "/ats/apply/B")
    fill_contact_and_uploads(page)
    page.get_by_label("Country", exact=True).select_option(label="United States")
    page.get_by_role("button", name="Review application", exact=True).click()
    expect(page.get_by_role("heading", name="Review mock application", exact=True)).to_be_visible()
    assert page.request.get(origin + "/ats/receipts/browser-multipage").json() == {"status": "not_found"}
    fill_confirmation(page, "browser-multipage")
    page.get_by_role("button", name="Submit mock application", exact=True).click()
    expect(page.get_by_role("heading", name="Thank you for applying.", exact=True)).to_be_visible()
    receipt = page.request.get(origin + "/ats/receipts/browser-multipage").json()
    assert receipt["submission_count"] == 1 and receipt["requisition"] == "B"
    assert page.request.get(origin + "/_test/request-counts").json() == {"review": 2, "submit": 1}
