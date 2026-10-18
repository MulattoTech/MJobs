"""Connected browser acceptance against the actual local Compose deployment."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx
from docx import Document
from playwright.sync_api import sync_playwright, expect
from reportlab.pdfgen import canvas

from cli.live_feedback import capture_redacted_screenshot
from cli.compose_recovery import collect_public_job_inventory
from cli import local_persistence_acceptance as persistence


def wait_for_mock_run(request, path, operation_id, *, timeout_seconds=210,
                      clock=time.monotonic, sleep=time.sleep):
    """Wait for this reviewed intent, without resubmitting or hiding worker failure."""
    deadline = clock() + timeout_seconds
    while True:
        result = request(path)
        if result.get("operation_id") != operation_id:
            raise RuntimeError("mock run identity changed")
        state = result.get("state")
        if state == "COMPLETED":
            return result
        if state not in {"QUEUED", "RUNNING"}:
            # Do not publish arbitrary worker diagnostics or untrusted state text.
            safe_state = state if state in {"FAILED", "CANCELLED", "BLOCKED", "DEAD_LETTERED"} else "UNKNOWN"
            raise RuntimeError(f"mock run did not complete: {safe_state}")
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError(f"mock run completion timed out: {state}")
        sleep(min(1, remaining))


def validate_onboarding_preflight(settings, health, readiness):
    if (settings.get("operation_mode") != "FIXTURE"
            or settings.get("external_submission_enabled") is not False
            or health.get("operation_mode") != "FIXTURE"
            or health.get("external_submission_enabled") is not False
            or readiness.get("status") != "ready"):
        raise RuntimeError("onboarding requires ready FIXTURE mode with external submission disabled")


def validate_onboarding_target(base_url):
    parsed = urlsplit(base_url)
    hostname = (parsed.hostname or "").lower()
    loopback = hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "http" or not loopback or parsed.username or parsed.password:
        raise RuntimeError("onboarding target must be an HTTP loopback Compose service")


def assert_onboarding_profile_facts(profile, pending_fact_ids):
    facts = profile["facts"]
    by_id = {row["fact_id"]: row for row in facts}
    pending_fact_ids = set(pending_fact_ids)
    assert len(by_id) == len(facts), "duplicate profile fact IDs"
    approved = [row for row in facts if row["verified"] and row["user_approved"]]
    if ({row["text"] for row in approved} != {"Python", "SQL"}
            or len(approved) != 2
            or any(row["kind"] != "skill" for row in approved)
            or any(row["fact_id"] in pending_fact_ids for row in approved)):
        raise AssertionError("unexpected approved facts in onboarding profile")
    pending = [row for row in facts if row["fact_id"] in pending_fact_ids]
    if (len(pending) != len(pending_fact_ids)
            or any(row["verified"] or row["user_approved"] for row in pending)
            or set(by_id) != pending_fact_ids | {row["fact_id"] for row in approved}):
        raise AssertionError("onboarding profile facts differ from reviewed fact set")
    return {row["text"]: row["fact_id"] for row in approved}


def assert_no_pending_facts_in_documents(documents, pending_fact_ids, approved_fact_ids):
    pending_fact_ids = set(pending_fact_ids)
    approved_fact_ids = set(approved_fact_ids)
    for document in documents:
        used_ids = set(document.get("fact_ids", []))
        facts_used = document.get("facts_used", [])
        used_ids.update(row.get("fact_id") for row in facts_used)
        if (used_ids & pending_fact_ids or used_ids - approved_fact_ids
                or any(not row.get("verified") or not row.get("user_approved")
                       for row in facts_used)):
            raise AssertionError("pending or unsupported facts reached generated documents")


def assert_search_scoped_discovery(request_payload, operation, jobs, *, profile_id,
                                   search_profile_id, expected_titles):
    """Check queued fixture discovery and durable readback for a selected saved search."""
    if request_payload.get("profile_id") != profile_id:
        raise AssertionError("discovery request changed the active profile")
    if request_payload.get("search_profile_id") != search_profile_id:
        raise AssertionError("discovery request omitted or changed the selected saved search")
    if operation.get("entity_id") != profile_id:
        raise AssertionError("discovery operation changed the active profile")
    params = operation.get("params") or {}
    if params.get("profile_id", profile_id) != profile_id:
        raise AssertionError("discovery operation parameters changed the active profile")
    if params.get("search_profile_id") != search_profile_id:
        raise AssertionError("discovery operation did not retain the selected saved search")
    if any(row.get("profile_id", profile_id) != profile_id for row in jobs):
        raise AssertionError("search-filtered readback included another profile's job")
    titles = [row.get("title") for row in jobs]
    if set(titles) != set(expected_titles) or len(titles) != len(expected_titles):
        raise AssertionError("search-filtered job readback differs from the reviewed criteria")


def _onboarding_fixture_pdf():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    text = pdf.beginText(40, 780)
    for line in (
        "Summary", "Fictional onboarding summary for extraction review only.",
    ):
        text.textLine(line)
    pdf.drawText(text)
    pdf.save()
    return stream.getvalue()


def _onboarding_preflight(base_url):
    validate_onboarding_target(base_url)
    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
        settings = client.get("/settings")
        settings.raise_for_status()
        health = client.get("/health")
        health.raise_for_status()
        readiness = client.get("/readiness")
        readiness.raise_for_status()
    validate_onboarding_preflight(settings.json(), health.json(), readiness.json())


def run(base_url, evidence_path, onboarding=False):
    if onboarding:
        # This read-only gate runs before output creation, Chromium launch, or any UI write.
        _onboarding_preflight(base_url)
    artifacts = Path("outputs/ui-e2e") / str(urlsplit(base_url).port)
    artifacts.mkdir(parents=True, exist_ok=True)
    actions = []
    job_inventory = None
    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
        unsafe_requests = []
        if onboarding:
            allowed_origin = urlsplit(base_url).netloc

            def local_fixture_route(route):
                current = route.request
                parsed = urlsplit(current.url)
                path = parsed.path.lower()
                blocked_path = (path.rstrip("/").endswith(("/apply", "/submit"))
                                or path in {"/apply", "/submit"}
                                or "/external-application" in path)
                application_write = (current.method != "GET" and "/applications/" in path)
                explicit_mock = path.endswith("/mock-run")
                allowed_application_write = path == "/applications" or explicit_mock
                if (parsed.netloc != allowed_origin or blocked_path
                        or application_write and not allowed_application_write):
                    unsafe_requests.append({"method": current.method, "url": current.url})
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", local_fixture_route)
        page = context.new_page()
        page.set_default_timeout(45000)
        failures = []
        page.on("pageerror", lambda failure: failures.append(str(failure)))

        def navigation(name):
            page.get_by_role("navigation").get_by_role("button", name=name).click()

        def request(path):
            response = context.request.get(base_url + path)
            assert response.ok, (path, response.status)
            return response.json()

        def download_document(document_id, format):
            path = f"/profiles/{profile_id}/documents/{document_id}/{format}"
            response = context.request.get(base_url + path)
            assert response.ok, (path, response.status)
            return response.body()

        def save_profile():
            page.get_by_role("button", name="Save profile", exact=True).click()
            expect(page.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()

        def fact_editor(text):
            for fieldset in page.locator("fieldset.fact").all():
                value = fieldset.get_by_role("textbox", name="Fact text", exact=True)
                if value.count() and value.input_value() == text:
                    return fieldset
            raise AssertionError("Expected exact profile fact text was not found: " + text)

        def prepare():
            if page.get_by_label("Order approved skills for this job", exact=True).is_checked():
                preview = page.get_by_role("region", name="Resume keyword alignment preview", exact=True)
                preview.get_by_role("button", name="Preview keyword alignment", exact=True).click()
                expect(preview.get_by_role("status")).to_contain_text("Read-only preview")
            page.get_by_role("button", name="Prepare documents", exact=True).click()
            expect(page.get_by_role("heading", name="Application details", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Run dry-run", exact=True)).to_be_enabled()

        def select_data_job():
            navigation("Jobs")
            page.get_by_label("Search jobs").fill("data")
            if onboarding:
                page.get_by_label("Job title", exact=True).fill("")
                page.get_by_label("Work location", exact=True).select_option("")
            card = page.get_by_role("button").filter(has=page.get_by_role("heading", name="Data Engineer", exact=True))
            expect(card).to_have_count(1)
            card.click()
            expect(page.get_by_role("heading", name="Why this match?", exact=True)).to_be_visible()

        try:
            page.goto(base_url, wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="Find your next opportunity.")).to_be_visible()
            pending_fact_ids = set()
            discovery_search_proofs = []
            if onboarding:
                page.get_by_role("button", name="New profile", exact=True).click()
                page.get_by_label("Full name", exact=True).fill("Synthetic Onboarding Applicant")
                page.get_by_label("Email", exact=True).fill("onboarding@example.invalid")
                with page.expect_response(lambda response:
                        response.request.method == "PUT"
                        and urlsplit(response.url).path == "/profiles") as profile_saved:
                    save_profile()
                assert profile_saved.value.status == 200, profile_saved.value.text()
                profile_id = profile_saved.value.json()["id"]
                expect(page.get_by_label("Active profile", exact=True)).to_have_value(profile_id)
                assert profile_id and not profile_id.startswith("demo-")
                profile = request("/profiles/" + profile_id)
                assert profile["facts"] == []
                actions.append({"action": "UI created a new synthetic profile without replacing existing profiles", "result": "PASS"})

                page.get_by_label("Choose a resume (up to 10 MiB)").set_input_files({
                    "name": "synthetic-onboarding.pdf", "mimeType": "application/pdf",
                    "buffer": _onboarding_fixture_pdf(),
                })
                page.get_by_label("Resume label", exact=True).fill("Synthetic onboarding fixture")
                page.get_by_role("button", name="Store resume", exact=True).click()
                resume_row = page.locator("[data-resume-version='1']")
                expect(resume_row).to_be_visible()
                resume_row.get_by_role("button", name="Extract and review", exact=True).click()
                review = page.get_by_role("region", name="Resume extraction review")
                expect(review).to_be_visible()
                suggestions = review.locator('label.fact-suggestion:has(strong) input[type="checkbox"]')
                assert suggestions.count() > 0
                for suggestion in suggestions.all():
                    expect(suggestion).not_to_be_checked()
                    suggestion.check()
                expect(review.get_by_role("button", name="Add selected to profile editor", exact=True)).to_be_enabled()
                review.get_by_role("button", name="Add selected to profile editor", exact=True).click()
                imported_rows = page.locator("fieldset.fact")
                expect(imported_rows).not_to_have_count(0, timeout=10000)
                for imported_fact in imported_rows.all():
                    expect(imported_fact).to_contain_text("Pending")
                    expect(imported_fact.get_by_label("I verified this fact", exact=True)).not_to_be_checked()
                    expect(imported_fact.get_by_label("I approve its use in documents", exact=True)).not_to_be_checked()
                save_profile()
                imported_profile = request("/profiles/" + profile_id)
                pending_fact_ids = {row["fact_id"] for row in imported_profile["facts"]}
                assert pending_fact_ids
                assert all(not row["verified"] and not row["user_approved"]
                           for row in imported_profile["facts"])
                actions.append({"action": "PDF imported through UI and extraction suggestions remain unverified and unapproved after readback", "result": "PASS"})

                for fact_text in ("Python", "SQL"):
                    page.get_by_role("button", name="Add fact", exact=True).click()
                    new_fact_editor = page.locator("fieldset.fact").last
                    new_fact_editor.get_by_role("combobox").select_option("skill")
                    new_fact_editor.get_by_label("Confidence (0–100)", exact=True).fill("100")
                    new_fact_editor.get_by_label("Fact text", exact=True).fill(fact_text)
                    new_fact_editor.get_by_label("Evidence / source", exact=True).fill("Explicitly reviewed synthetic onboarding fact")
                    new_fact_editor.get_by_label("I verified this fact", exact=True).check()
                    new_fact_editor.get_by_label("I approve its use in documents", exact=True).check()
                for question, answer_text in (
                    ("Country", "United States"),
                    ("Remote role", "yes"),
                    ("Approved fixture answer", "Controlled fictional test application."),
                ):
                    page.get_by_role("button", name="Add answer", exact=True).click()
                    answer_editor = page.locator("fieldset.fact").last
                    answer_editor.get_by_role("textbox", name="Question this answer addresses", exact=True).fill(question)
                    answer_editor.get_by_role("textbox", name="Answer text", exact=True).fill(answer_text)
                    answer_editor.get_by_label("Provenance", exact=True).fill("Explicit synthetic mock-form review")
                    answer_editor.get_by_label("Confidence (0–100)", exact=True).fill("100")
                    if question != "Approved fixture answer":
                        answer_editor.get_by_label("I reviewed and verified this answer", exact=True).check()
                        answer_editor.get_by_label("I approve exact reuse for this question", exact=True).check()
                save_profile()
                profile = request("/profiles/" + profile_id)
                assert len(profile["answers"]) == 3
                answer_by_question = {row["question"]: row for row in profile["answers"]}
                assert {"Country", "Remote role", "Approved fixture answer"} == set(answer_by_question)
                assert all(answer_by_question[question]["verified"]
                           and answer_by_question[question]["user_approved"]
                           and answer_by_question[question]["confidence"] == 100
                           for question in ("Country", "Remote role"))
                assert not answer_by_question["Approved fixture answer"]["verified"]
                assert not answer_by_question["Approved fixture answer"]["user_approved"]
                assert not {row["id"] for row in profile["answers"]} & {"country", "remote", "fixture_question"}
                actions.append({"action": "UI saves two explicitly approved synthetic answers and keeps the required mock-form answer unreviewed for later question review", "result": "PASS"})
                skill_fact_ids = assert_onboarding_profile_facts(profile, pending_fact_ids)
                python_fact_id = skill_fact_ids["Python"]
                sql_fact_id = skill_fact_ids["SQL"]
                actions.append({"action": "Only explicitly entered and reviewed Python and SQL facts are verified and approved", "result": "PASS"})

                navigation("Jobs")
                page.get_by_label("Search jobs").fill("IT")
                page.get_by_label("Job title", exact=True).fill("IT Support Specialist")
                page.get_by_label("Work location", exact=True).select_option("REMOTE")
                page.get_by_label("Saved search name", exact=True).fill("Synthetic remote IT onboarding search")
                page.get_by_role("button", name="Save current filters", exact=True).click()
                expect(page.get_by_role("status").filter(has_text="Saved Jobs filters")).to_be_visible()
                saved_searches = request("/search-profiles?profile_id=" + profile_id)["search_profiles"]
                saved_search = next(row for row in saved_searches
                                    if row["name"] == "Synthetic remote IT onboarding search")
                assert saved_search["profile_id"] == profile_id
                assert saved_search["criteria"]["job_filters"]["remote_type"] == "REMOTE"
                assert saved_search["criteria"]["job_filters"]["q"] == "IT"
                assert saved_search["criteria"]["job_filters"]["title"] == "IT Support Specialist"
                assert saved_search["source_ids"] == []
                assert saved_search["enabled"] is False
                expect(page.get_by_label("Saved search profile", exact=True).last).to_have_value(saved_search["id"])

                # Resume the disabled saved search through the public Sources control.
                navigation("Sources")
                with page.expect_response(lambda candidate:
                        candidate.request.method == "PUT"
                        and urlsplit(candidate.url).path == "/search-profiles") as resumed_search_response:
                    page.get_by_role("button", name="Resume search", exact=True).click()
                assert resumed_search_response.value.status == 200
                saved_search = request("/search-profiles/" + saved_search["id"])
                assert saved_search["profile_id"] == profile_id and saved_search["enabled"] is True
                with page.expect_response(lambda response:
                        response.request.method == "POST"
                        and urlsplit(response.url).path == "/discovery") as discovery_response:
                    page.get_by_role("button", name="Collect local fixture jobs", exact=True).click()
                response = discovery_response.value
                assert response.status == 202, response.text()
                discovery_request = response.request.post_data_json
                discovery_request_id = discovery_request["request_id"]
                assert discovery_request["search_profile_id"] == saved_search["id"]
                discovery_operation_id = response.json()["operation_id"]
                discovery_deadline = time.monotonic() + 120
                while time.monotonic() < discovery_deadline:
                    discovery_operation = request("/operations/" + discovery_operation_id)
                    if discovery_operation["state"] in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
                        break
                    time.sleep(0.5)
                assert discovery_operation["state"] == "COMPLETED", discovery_operation
                assert discovery_operation["params"]["request_id"] == discovery_request_id
                nonmatching_jobs = request("/jobs?profile_id=" + profile_id
                                            + "&search_profile_id=" + saved_search["id"])["jobs"]
                assert_search_scoped_discovery(
                    discovery_request, discovery_operation, nonmatching_jobs,
                    profile_id=profile_id, search_profile_id=saved_search["id"],
                    expected_titles=set(),
                )
                assert {row["title"] for row in request("/jobs?profile_id=" + profile_id)["jobs"]} >= {
                    "Python Developer", "Data Engineer"}
                discovery_search_proofs.append({
                    "search_profile_id": saved_search["id"],
                    "operation_id": discovery_operation_id,
                    "profile_id": profile_id,
                    "filtered_titles": [],
                    "result": "PASS",
                })
                actions.append({"action": "Resumed the saved remote IT search in Sources; actual fixture discovery completed for its selected same-profile scope and returned an empty filtered result because neither fixture job matches IT Support", "result": "PASS", "boundary": "Sources UI → queued /discovery → Compose source worker → selected-search jobs API"})

                # Use the actual matching synthetic fixture for a positive filter result.
                navigation("Jobs")
                page.get_by_label("Search jobs").fill("data")
                page.get_by_label("Job title", exact=True).fill("Data Engineer")
                page.get_by_label("Work location", exact=True).select_option("REMOTE")
                page.get_by_label("Saved search name", exact=True).fill(
                    "Synthetic remote data engineering onboarding search")
                page.get_by_role("button", name="Save current filters", exact=True).click()
                expect(page.get_by_role("status").filter(has_text="Saved Jobs filters")).to_be_visible()
                matching_search = next(row for row in request(
                    "/search-profiles?profile_id=" + profile_id)["search_profiles"]
                    if row["name"] == "Synthetic remote data engineering onboarding search")
                assert matching_search["profile_id"] == profile_id
                assert matching_search["criteria"]["job_filters"]["q"] == "data"
                assert matching_search["criteria"]["job_filters"]["title"] == "Data Engineer"
                assert matching_search["criteria"]["job_filters"]["remote_type"] == "REMOTE"
                expect(page.get_by_label("Saved search profile", exact=True).last).to_have_value(matching_search["id"])
                navigation("Sources")
                with page.expect_response(lambda candidate:
                        candidate.request.method == "PUT"
                        and urlsplit(candidate.url).path == "/search-profiles") as matching_resume_response:
                    page.get_by_role("button", name="Resume search", exact=True).click()
                assert matching_resume_response.value.status == 200
                matching_search = request("/search-profiles/" + matching_search["id"])
                assert matching_search["profile_id"] == profile_id and matching_search["enabled"] is True
                with page.expect_response(lambda candidate:
                        candidate.request.method == "POST"
                        and urlsplit(candidate.url).path == "/discovery") as matching_response:
                    page.get_by_role("button", name="Collect local fixture jobs", exact=True).click()
                response = matching_response.value
                assert response.status == 202, response.text()
                matching_request = response.request.post_data_json
                assert matching_request["search_profile_id"] == matching_search["id"]
                matching_request_id = matching_request["request_id"]
                discovery_operation_id = response.json()["operation_id"]
                discovery_deadline = time.monotonic() + 120
                while time.monotonic() < discovery_deadline:
                    discovery_operation = request("/operations/" + discovery_operation_id)
                    if discovery_operation["state"] in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
                        break
                    time.sleep(0.5)
                assert discovery_operation["state"] == "COMPLETED", discovery_operation
                assert discovery_operation["params"]["request_id"] == matching_request_id
                matching_jobs = request("/jobs?profile_id=" + profile_id
                                         + "&search_profile_id=" + matching_search["id"])["jobs"]
                assert_search_scoped_discovery(
                    matching_request, discovery_operation, matching_jobs,
                    profile_id=profile_id, search_profile_id=matching_search["id"],
                    expected_titles={"Data Engineer"},
                )
                discovery_job_ids = {row["id"] for row in matching_jobs}
                assert discovery_job_ids, "matching saved search returned no fixture jobs"
                discovery_search_proofs.append({
                    "search_profile_id": matching_search["id"],
                    "operation_id": discovery_operation_id,
                    "profile_id": profile_id,
                    "filtered_titles": [row["title"] for row in matching_jobs],
                    "result": "PASS",
                })
                actions.append({"action": "Selected a matching remote Data Engineer search; actual fixture worker completed and only the matching job appeared in same-profile filtered readback", "result": "PASS", "boundary": "Jobs UI → Sources UI → queued /discovery → Compose source worker → selected-search jobs API"})
                history = request("/profiles/" + profile_id + "/search-history")
                assert history["profile_id"] == profile_id
                history_item = next(item for item in history["items"]
                                    if item["id"].startswith(discovery_operation_id + ":"))
                assert history_item["status"] == "COMPLETED"
                assert history_item["profile_id"] == profile_id
                assert history_item["source"] == "mock-fixture"
                assert history_item["results"] and history_item["results"] > 0
                search_after_discovery = next(row for row in request(
                    "/search-profiles?profile_id=" + profile_id)["search_profiles"]
                    if row["id"] == matching_search["id"])
                assert search_after_discovery["criteria"]["job_filters"]["remote_type"] == "REMOTE"
                assert search_after_discovery["source_ids"] == []
            else:
                page.get_by_role("button", name="Load fictional demo", exact=True).click()
                expect(page.get_by_role("status").filter(has_text="Fictional demo loaded")).to_be_visible()
                profile_id = page.get_by_label("Active profile").input_value()
                assert profile_id.startswith("demo-")
                profile = request("/profiles/" + profile_id)
                python_fact_id, sql_fact_id = "python", "sql"
            select_data_job()
            expect(page.locator(".big-score")).to_have_text("100% · Exceptional")
            actions.append({"action": "Explicit fictional demo, connected search, match explanation and raw provenance", "result": "PASS"})

            navigation("Profile")
            fact_editor("SQL").get_by_label("I approve its use in documents", exact=True).uncheck()
            save_profile()
            select_data_job()
            expect(page.locator(".big-score")).to_have_text("50% · Weak")
            prepare()
            application = request("/applications?profile_id=" + profile_id)["applications"][0]
            identifier = application["id"]
            if onboarding:
                assert application["job_id"] in discovery_job_ids, "selected application job was not returned by this profile's fixture discovery"
            assert application["fact_ids"] == [python_fact_id]
            first_documents = request("/profiles/" + profile_id + "/applications/" + identifier + "/documents")["documents"]
            first_resume = next(row for row in first_documents if row["kind"] == "resume")
            assert first_resume['created_at'] and first_resume['generation_model'] == 'deterministic-fact-copy-v2'
            assert [fact['text'] for fact in first_resume['facts_used']] == ['Python']
            provenance = page.get_by_role('region', name='resume version 1', exact=True)
            provenance.get_by_text('Generation details and facts', exact=True).click()
            expect(provenance.get_by_text('Generation method: deterministic-fact-copy-v2', exact=True)).to_be_visible()
            with page.expect_download() as text_download:
                provenance.get_by_role('link', name='Download TXT', exact=True).click()
            text_bytes = Path(text_download.value.path()).read_bytes()
            assert hashlib.sha256(text_bytes).hexdigest() == first_resume['sha256']['txt']
            original = download_document(first_resume["id"], "docx")
            text = "\n".join(row.text for row in Document(io.BytesIO(original)).paragraphs)
            assert "Python" in text and "SQL" not in text
            assert first_resume['template']=='verified-headings-v1'
            assert [p.text for p in Document(io.BytesIO(original)).paragraphs if p.style.name=='Heading 1']==['Skills']
            actions.append({'action':'ATS resume templates expose conventional headings in the browser-downloaded DOCX while preserving approved wording','result':'PASS'})
            assert text_bytes.decode('utf-8').strip() == text
            actions.append({'action':'Original approved facts, generation metadata and checksum-verified TXT download visible through the browser', 'result':'PASS'})
            actions.append({"action": "Pending fact excluded from matching and downloaded factual DOCX", "result": "PASS"})

            navigation("Profile")
            fact_editor("SQL").get_by_label("I approve its use in documents", exact=True).check()
            save_profile()
            select_data_job()
            expect(page.locator(".big-score")).to_have_text("100% · Exceptional")
            prepare()
            revised = request("/profiles/" + profile_id + "/applications/" + identifier)
            assert revised["document_version"] == 2 and set(revised["fact_ids"]) == {python_fact_id, sql_fact_id}
            documents = request("/profiles/" + profile_id + "/applications/" + identifier + "/documents")["documents"]
            assert len(documents) == 4
            old = download_document(first_resume["id"], "docx")
            assert old == original
            with page.expect_download() as download:
                page.get_by_role("link", name="Download PDF", exact=True).first.click()
            pdf_path = artifacts / "fictional-document.pdf"
            download.value.save_as(pdf_path)
            assert pdf_path.read_bytes().startswith(b"%PDF")
            assert hashlib.sha256(pdf_path.read_bytes()).hexdigest() in {doc["sha256"]["pdf"] for doc in documents}
            actions.append({"action": "Approved profile revision generates new documents, preserves original bytes, and PDF downloads through UI", "result": "PASS"})

            select_data_job()
            page.get_by_label('Order approved skills for this job',exact=True).check()
            prepare()
            tailored=next(row for row in request('/profiles/'+profile_id+'/applications/'+identifier+'/documents')['documents'] if row['version']==3 and row['kind']=='resume')
            assert tailored['generation_model']=='deterministic-target-relevant-bullets-summary-skill-responsibility-order-v4'
            assert tailored['template']=='approved-target-relevant-bullets-summary-skill-responsibility-and-bullet-emphasis-v5'
            assert tailored['generation_context']['emphasized_fact_ids']
            expect(page.get_by_role('region',name='resume version 3',exact=True).get_by_text('Job-matched approved facts in bold: 2.',exact=False)).to_be_visible()
            emphasized_doc=Document(io.BytesIO(download_document(tailored['id'],'docx')))
            assert any(p.text=='Python' and all(r.bold for r in p.runs) for p in emphasized_doc.paragraphs)
            actions.append({'action':'Job-matched technology emphasis reaches worker, DOCX download and visible provenance','result':'PASS'})
            assert tailored['generation_context']['mode']=='APPROVED_SKILL_ORDER'
            assert set(tailored['fact_ids'])=={python_fact_id,sql_fact_id}
            expect(page.get_by_role('region',name='resume version 3',exact=True).get_by_text('Tailored resume: skill order',exact=False)).to_be_visible()
            actions.append({'action':'Explicit skill-order opt-in reaches the document worker and preserves approved facts with target snapshot and separate revision','result':'PASS'})

            comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
            comparison.get_by_label('Base resume',exact=True).select_option(label='v2')
            comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
            result=comparison.get_by_role('region',name='Resume comparison result',exact=True)
            expect(result.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('No content omitted.')
            expect(result.get_by_role('region',name='Summary changes',exact=True)).to_contain_text('No changes.')
            expect(result.get_by_role('heading',name='Generated base · v2',exact=True)).to_be_visible()
            expect(result.get_by_role('heading',name='Tailored resume · v3',exact=True)).to_be_visible()
            actions.append({'action':'Immutable generated base and tailored resume comparison shows recorded versions and unchanged facts through the UI','result':'PASS'})

            profile_before_comparison=request('/profiles/'+profile_id)
            uploaded_original_text=b'Summary\nOriginal synthetic summary\nSkills\nSQL; Python\nResponsibilities\nMaintained synthetic servers\n'
            uploaded_response=context.request.post(base_url+'/profiles/'+profile_id+'/resumes', data=uploaded_original_text,
                headers={'X-Filename':'comparison-base.txt','Content-Type':'text/plain'})
            assert uploaded_response.status==201, uploaded_response.text()
            uploaded_original=uploaded_response.json()
            comparison.get_by_role('button',name='Refresh base resumes',exact=True).click()
            comparison.get_by_label('Base resume',exact=True).select_option('u:'+uploaded_original['revision_id'])
            comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
            expect(result.get_by_role('heading',name='Uploaded base · v1',exact=True)).to_be_visible()
            expect(result.get_by_role('region',name='Summary changes',exact=True)).to_contain_text('Original synthetic summary')
            expect(result.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('Maintained synthetic servers')
            expect(result.get_by_role('region',name='Original extraction provenance',exact=True)).to_contain_text('Native extracted text')
            with page.expect_download() as original_download:
                result.get_by_role('link',name='Download compared original',exact=True).click()
            assert Path(original_download.value.path()).read_bytes()==uploaded_original_text
            assert request('/profiles/'+profile_id)==profile_before_comparison
            actions.append({'action':'Uploaded original comparison preserves revision/hash and profile facts, displays changed summary and omitted content, and downloads exact source bytes','result':'PASS'})

            select_data_job()
            page.get_by_label('Order approved skills for this job',exact=True).check()
            page.get_by_label('Choose resume facts',exact=True).check()
            expect(page.get_by_label('Include SQL',exact=True)).to_be_checked()
            page.get_by_label('Include SQL',exact=True).uncheck()
            prepare()
            selected_documents=request('/profiles/'+profile_id+'/applications/'+identifier+'/documents')['documents']
            selected_resume=next(row for row in selected_documents if row['version']==4 and row['kind']=='resume')
            selected_cover=next(row for row in selected_documents if row['version']==4 and row['kind']=='cover-letter')
            assert selected_resume['generation_model']=='deterministic-fact-selection-and-bullet-emphasis-v1'
            assert selected_resume['fact_ids']==[python_fact_id] and set(selected_cover['fact_ids'])=={python_fact_id,sql_fact_id}
            assert selected_resume['generation_context']['omitted_fact_ids']==[sql_fact_id]
            selected_history=page.get_by_role('region',name='resume version 4',exact=True)
            expect(selected_history.get_by_text('Tailored resume: selected facts',exact=False)).to_be_visible()
            with page.expect_download() as selected_download:
                selected_history.get_by_role('link',name='Download TXT',exact=True).click()
            selected_text=Path(selected_download.value.path()).read_text(encoding='utf-8')
            assert 'Python' in selected_text and 'SQL' not in selected_text
            assert 'SQL' in download_document(selected_cover['id'],'txt').decode('utf-8')
            comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
            comparison.get_by_label('Base resume',exact=True).select_option(label='v2')
            comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
            expect(comparison.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('skill: SQL')
            assert request('/profiles/'+profile_id)==profile_before_comparison
            actions.append({'action':'Explicit approved resume selection reaches the real document worker, omits SQL only from the resume, preserves profile and cover letter, and shows immutable omissions','result':'PASS'})

            navigation('Profile')
            page.get_by_label('city',exact=True).fill('Tulsa')
            save_profile()
            saved_contact_profile=request('/profiles/'+profile_id)
            select_data_job()
            page.get_by_label('Order approved skills for this job',exact=True).uncheck()
            contact_option=page.get_by_label('Include saved resume contact details',exact=True)
            expect(contact_option).not_to_be_checked();contact_option.check()
            prepare()
            contact_resume=next(row for row in request('/profiles/'+profile_id+'/applications/'+identifier+'/documents')['documents'] if row['version']==5 and row['kind']=='resume')
            assert contact_resume['template']=='approved-contact-base-v1'
            assert 'Location: Tulsa' in contact_resume['generation_context']['contact_header']
            contact_history=page.get_by_role('region',name='resume version 5',exact=True)
            contact_history.get_by_text('Generation details and facts',exact=True).click()
            expect(contact_history.get_by_role('region',name='Saved resume contact header')).to_contain_text('Location: Tulsa')
            with page.expect_download() as contact_download:
                contact_history.get_by_role('link',name='Download TXT',exact=True).click()
            assert 'Location: Tulsa' in Path(contact_download.value.path()).read_text(encoding='utf-8')
            assert 'Location: Tulsa' not in download_document(selected_resume['id'],'txt').decode('utf-8')
            assert request('/profiles/'+profile_id)==saved_contact_profile
            actions.append({'action':'Explicit saved contact header reaches real document worker, history and download while prior revision and profile remain unchanged','result':'PASS'})

            # Deliberately leave one synthetic required answer uncertain through
            # preparation. The public dry-run must surface it for review before
            # this same application can be resumed and prepared at new profile pins.
            page.get_by_role("button", name="Run dry-run", exact=True).click()
            expect(page.get_by_role("status", name="Application state", exact=True)).to_have_text(
                "REVIEW_REQUIRED", timeout=30000)
            preflight = page.get_by_role("region", name="ATS question preflight", exact=True)
            expect(preflight.get_by_role("list", name="Unresolved required questions")).to_contain_text(
                "Approved fixture answer")
            previous_profile = request("/profiles/" + profile_id)
            review_row = preflight.get_by_role("list", name="Unresolved required questions").locator(
                "li").filter(has_text="Approved fixture answer")
            review_row.get_by_role("button", name="Review this question", exact=True).click()
            answer_group = page.get_by_role("region", name="Application answers", exact=True).get_by_role(
                "group", name="Question 1")
            answer_group.get_by_role("textbox", name="Answer", exact=True).fill(
                "Controlled fictional test application, reviewed for this job.")
            answer_group.get_by_label("I reviewed this exact answer for this job", exact=True).check()
            answer_group.get_by_role("button", name="Save to Answer Bank", exact=True).click()
            answer_confirmation = answer_group.get_by_role(
                "region", name="Answer Bank confirmation for question 1", exact=True)
            answer_confirmation.get_by_label(
                "I confirm this answer is accurate and may be reused", exact=True).check()
            with page.expect_response(lambda response:
                    response.request.method == "PUT"
                    and urlsplit(response.url).path == "/profiles") as reviewed_answer_save:
                answer_confirmation.get_by_role(
                    "button", name="Confirm and save to Answer Bank", exact=True).click()
            assert reviewed_answer_save.value.status == 200, reviewed_answer_save.value.text()
            updated_profile = request("/profiles/" + profile_id)
            reviewed_answer = next(row for row in updated_profile["answers"]
                                   if row["question"] == "Approved fixture answer")
            assert reviewed_answer["text"] == "Controlled fictional test application, reviewed for this job."
            assert reviewed_answer["verified"] and reviewed_answer["user_approved"]
            assert updated_profile["version"] > previous_profile["version"]
            old_pins = request("/profiles/" + profile_id + "/applications/" + identifier)
            assert old_pins["profile_version"] < updated_profile["version"]
            old_document_version = old_pins["document_version"]
            with page.expect_response(lambda candidate:
                    candidate.request.method == "POST"
                    and urlsplit(candidate.url).path.endswith("/reprepare")) as reprepare_response:
                page.get_by_role("button", name="Resume and prepare current revision", exact=True).click()
            assert reprepare_response.value.status == 200, reprepare_response.value.text()
            expect(page.get_by_role("status").filter(
                has_text="Run a local dry run to create a fresh ATS question preflight")).to_be_visible()
            resumed = request("/profiles/" + profile_id + "/applications/" + identifier)
            assert resumed["profile_version"] == updated_profile["version"]
            assert resumed["document_version"] == old_document_version + 1
            assert resumed["id"] == identifier and resumed["profile_id"] == profile_id
            revised_documents = request("/profiles/" + profile_id + "/applications/" + identifier + "/documents")["documents"]
            assert any(row["version"] == old_document_version for row in revised_documents)
            assert any(row["version"] == resumed["document_version"]
                       and row["profile_version"] == updated_profile["version"]
                       for row in revised_documents)
            assert resumed.get("question_preflight") is not None
            assert resumed["question_preflight"]["is_current"] is False
            actions.append({"action": "Unknown required answer stopped dry-run; explicit question review changed approved profile facts, then explicit Resume prepared a new immutable document revision and retained old history", "result": "PASS", "boundary": "Connected React UI → public profile/reprepare APIs → actual document worker"})

            page.get_by_role("button", name="Run dry-run", exact=True).click()
            expect(page.get_by_role("status", name="Application state", exact=True)).to_have_text(
                "DRY_RUN_COMPLETE", timeout=30000)
            dry = request("/profiles/" + profile_id + "/applications/" + identifier)
            assert "STOP_BEFORE_SUBMIT" in dry["trace"] and "SUBMIT" not in dry["trace"]
            page.get_by_role("button", name="Start a new reviewed run", exact=True).click()
            page.get_by_label("I approve one fictional mock submission").check()
            mock_run_path = f"/profiles/{profile_id}/applications/{identifier}/mock-run"
            with page.expect_response(lambda response: response.request.method == "POST"
                                      and response.url == base_url + mock_run_path) as admission:
                page.get_by_role("button", name="Submit to local mock", exact=True).click()
            assert admission.value.status == 202
            admitted = admission.value.json()
            admitted_intent = page.evaluate("([profile, application]) => JSON.parse(localStorage.getItem('mjobs-mock-run-v1:' + encodeURIComponent(profile) + ':' + encodeURIComponent(application)))", [profile_id, identifier])
            assert admitted_intent["submit"] is True
            wait_for_mock_run(request, mock_run_path + "s/" + admitted_intent["requestId"],
                              admitted["operation_id"])
            expect(page.get_by_role("status", name="Application state", exact=True)).to_have_text("CONFIRMED")
            confirmed = request("/profiles/" + profile_id + "/applications/" + identifier)
            assert confirmed["target"] == "MOCK" and confirmed["confirmation"]["submission_count"] == 1
            assert request("/health")["external_submission_enabled"] is False
            controls = page.get_by_role('region', name='Recoverable mock run', exact=True)
            expect(controls.get_by_role('status').filter(has_text='Operation')).to_contain_text('COMPLETED')
            intent = page.evaluate("([profile, application]) => JSON.parse(localStorage.getItem('mjobs-mock-run-v1:'+encodeURIComponent(profile)+':'+encodeURIComponent(application)))", [profile_id, identifier])
            assert intent['submit'] is True
            repeated_response = context.request.post(base_url + "/profiles/" + profile_id + "/applications/" + identifier + "/mock-run", data={"submit": True, "request_id": intent['requestId']})
            assert repeated_response.status == 202
            repeated = repeated_response.json()
            assert repeated["state"] == "COMPLETED"
            assert repeated['replayed'] is True
            assert request("/profiles/" + profile_id + "/applications/" + identifier)["confirmation"] == confirmed["confirmation"]
            capture_redacted_screenshot(page, artifacts / "application-confirmed.png")
            actions.append({"action": "Browser dry-run stops before submission; explicit local mock receipt count remains one on repeat", "result": "PASS"})

            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_label("Active profile")).to_have_value(profile_id)
            navigation("Applications")
            page.get_by_role("button").filter(has=page.get_by_role("heading", name="Data Engineer", exact=True)).click()
            expect(page.get_by_role("status", name="Application state", exact=True)).to_have_text("CONFIRMED")
            navigation('Profile')
            base_resumes_before_library=request('/profiles/'+profile_id+'/resumes')['resumes']
            library=page.get_by_role('region', name='Document library', exact=True)
            library.get_by_label('Document type', exact=True).select_option('CERTIFICATION')
            library.get_by_label('Document label', exact=True).fill('Synthetic certificate')
            library.get_by_label('Choose a document', exact=False).set_input_files(
                {'name':'synthetic-certificate.txt','mimeType':'text/plain','buffer':b'Synthetic library proof, not a career credential.'})
            library.get_by_role('button', name='Store document', exact=True).click()
            collection=library.get_by_role('region',name='Synthetic certificate collection',exact=True)
            expect(collection.get_by_role('link',name='Download original version 1',exact=True)).to_be_visible()
            stored=request('/profiles/'+profile_id+'/document-library')['uploaded']
            certificate=next(row for row in stored if row['document_kind']=='CERTIFICATION')
            with page.expect_download() as download:
                collection.get_by_role('link',name='Download original version 1',exact=True).click()
            assert hashlib.sha256(Path(download.value.path()).read_bytes()).hexdigest()==certificate['sha256']
            assert request('/profiles/'+profile_id+'/resumes')['resumes']==base_resumes_before_library
            if onboarding:
                assert_onboarding_profile_facts(request('/profiles/'+profile_id), pending_fact_ids)
                assert_no_pending_facts_in_documents(
                    request('/profiles/'+profile_id+'/applications/'+identifier+'/documents')['documents'],
                    pending_fact_ids, {python_fact_id, sql_fact_id})
            else:
                assert {row['fact_id'] for row in request('/profiles/'+profile_id)['facts']}=={'python','sql'}
            actions.append({'action':'Typed document library stores/downloads checksum-backed certification without adding resume-import entries or career facts','result':'PASS'})
            navigation("Activity")
            expect(page.get_by_role("heading", name="Service health", exact=True)).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"
            capture_redacted_screenshot(page, artifacts / "activity-mobile.png")
            assert not failures, failures
            assert not unsafe_requests, unsafe_requests
            actions.append({"action": "Browser reload restores selected profile and application history; responsive Activity has no horizontal overflow or browser exceptions", "result": "PASS"})
            def read_bytes(path):
                response = context.request.get(base_url + path)
                assert response.ok, 'Persistence download failed'
                return response.body()
            sources = request('/company-sources')['company_sources']
            assert sources, 'Persistence proof requires a seeded company source'
            pinned = context.request.put(base_url + '/company-sources/' + sources[0]['id'] + '/pin',
                                         data={'pinned': True})
            assert pinned.ok, 'Synthetic company source control did not persist'
            assert next(row for row in request('/company-sources')['company_sources']
                        if row['id'] == sources[0]['id'])['priority'] == 'USER_PINNED'
            local_persistence = persistence.collect(request, read_bytes, profile_id, identifier)
            if onboarding:
                # Capture the public same-profile jobs/source-copy baseline while
                # the Playwright API request context is still open. The connected
                # recovery workflow compares this exact inventory after restart.
                job_inventory = collect_public_job_inventory(request, profile_id)
        except BaseException:
            capture_redacted_screenshot(page, artifacts / "failure.png")
            raise
        finally:
            context.close()
            browser.close()
    evidence = {"result": "PARTIAL" if onboarding else "PASS", "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": "python -m cli.ui_e2e --base-url " + base_url + (" --onboarding" if onboarding else ""), "target": "Connected React UI / PostgreSQL / three RQ queues / actual Chromium",
        "profile_id": profile_id, "application_id": identifier, "operation_id": repeated["operation_id"], "request_id": intent['requestId'], "confirmation": confirmed["confirmation"], "external_submission_enabled": False,
        "mode": "ONBOARDING_FIXTURE" if onboarding else "FICTIONAL_DEMO",
        "local_persistence": local_persistence,
        **({"job_inventory": job_inventory} if onboarding else {}),
        "discovery_operation_id": discovery_operation_id if onboarding else None,
        "discovery_request_id": discovery_request_id if onboarding else None,
        "discovery_search_proofs": discovery_search_proofs if onboarding else [],
        "limitations": ([
            "This onboarding journey uses synthetic fixture jobs and does not prove real-provider browser discovery (criterion 11).",
            "Source identity/deduplication and live public-source proof are outside this local fixture run.",
        ] if onboarding else []),
        "actions": actions, "artifacts": [str(path) for path in artifacts.glob("*") if path.name != "failure.png"]}
    Path(evidence_path).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18700")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--onboarding", action="store_true",
                        help="Run the connected UI acceptance from a new synthetic profile in ready FIXTURE mode")
    options = parser.parse_args()
    run(options.base_url, options.evidence, onboarding=options.onboarding)
