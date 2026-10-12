"""Connected browser acceptance for actionable unresolved application questions."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
QUESTION = "Describe your experience managing Azure subscriptions."
SENSITIVE_QUESTION = "Are you legally authorized to work in the United States?"


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationReviewQueueBrowserTests(unittest.TestCase):
    def test_unresolved_question_routes_to_review_and_saves_only_with_explicit_consent(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-review-queue-") as directory:
            root = Path(directory)
            database = root / "browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE, answers=[{
                "id": "azure-answer", "question": QUESTION,
                "text": "I managed Azure subscriptions in a fictional lab.",
                "source": "Synthetic profile", "confidence": 92,
                "verified": True, "user_approved": True,
            }, {
                "id": "azure-alternate-answer",
                "question": "Describe your experience managing Azure subscriptions at work.",
                "text": "An alternate synthetic answer from the profile bank.",
                "source": "Synthetic alternate profile", "confidence": 91,
                "verified": True, "user_approved": True,
            }]))
            store.save_profile(dict(PROFILE, id="other-profile", name="Other synthetic applicant",
                                    email="other@example.invalid", answers=[]))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            from services.applications.preflight import persist_preflight
            persist_preflight(store, application_id, {
                "result": "FAIL", "reason": "UNRESOLVED_REQUIRED_QUESTIONS",
                "observed_at": "2026-10-08T00:00:00Z",
                "profile_version": store.application(application_id)["profile_version"],
                "document_version": store.application(application_id)["document_version"],
                "route_id": store.application(application_id)["route_id"],
                "provider": "mock-ats", "tenant": "example", "requisition": "B",
                "metadata_fingerprint": "a" * 64, "mappings": [],
                "missing": [
                    {"question_id": "azure-experience", "question": QUESTION,
                     "fact_ids": [], "answer_ids": ["azure-proof"],
                     "profile_paths": [], "document_kinds": []},
                    {"question_id": "authorization", "question": SENSITIVE_QUESTION,
                     "fact_ids": [], "answer_ids": ["work_authorization"],
                     "profile_paths": [], "document_kinds": []},
                ],
            })
            from services.applications.preflight import latest_preflight
            self.assertEqual(latest_preflight(store, application_id)["result"], "FAIL")
            application_before = store.application(application_id)
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated application review API did not start")
                    time.sleep(0.02)
                outside, errors, writes = [], [], []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            outside.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda request: writes.append(request.method)
                            if request.method not in {"GET", "HEAD", "OPTIONS"} else None)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile")).to_have_value("fixture")
                    page.get_by_role("button", name="Review Queue", exact=False).click()
                    queue = page.get_by_role("region", name="Application review queue")
                    expect(queue).to_contain_text(job["company"])
                    expect(queue).to_contain_text("UNRESOLVED_REQUIRED_QUESTIONS")
                    expect(queue).to_contain_text(QUESTION)
                    expect(queue).to_contain_text("answers: azure-proof")
                    expect(queue).not_to_contain_text("CAPTCHA —")
                    queue.get_by_role("button", name=f"Review application: {job['title']} at {job['company']}").click()
                    self.assertFalse(writes, "Opening or refreshing the review queue must not mutate application state")

                    preflight = page.get_by_role("region", name="ATS question preflight")
                    unresolved = preflight.get_by_role("list", name="Unresolved required questions")
                    expect(unresolved).to_contain_text(QUESTION)
                    expect(unresolved).to_contain_text("answers: azure-proof")
                    expect(preflight).to_contain_text("UNRESOLVED_REQUIRED_QUESTIONS")
                    expect(unresolved).to_contain_text("Persisted preflight is missing answers: azure-proof")
                    route = unresolved.get_by_role("button", name="Review this question")
                    route.first.click()
                    route.first.click()
                    expect(page.get_by_label("Employer question")).to_have_count(1)
                    expect(page.get_by_label("Employer question").first).to_have_value(QUESTION)

                    # Existing suggestion provenance remains visible and the review stays per-job.
                    answer_review = page.get_by_role("region", name="Application answers", exact=True).get_by_role("group", name="Question 1", exact=True)
                    expect(answer_review).to_contain_text("Exact normalized match")
                    expect(answer_review).to_contain_text("match score 100%")
                    expect(answer_review).to_contain_text("Synthetic profile")
                    answer_review.get_by_role("button", name="Use saved answer (review required)").first.click()
                    answer_review.get_by_role("textbox", name="Answer", exact=True).fill(
                        "In a synthetic lab, I managed Azure subscriptions and reviewed access scopes.")
                    page.get_by_label("I reviewed this exact answer for this job", exact=True).check()
                    answer_review.get_by_role("button", name="Save to Answer Bank").click()
                    expect(answer_review.get_by_label("I confirm this answer is accurate and may be reused", exact=True)).to_be_visible()
                    answer_review.get_by_label("Answer bank confidence (%)", exact=True).fill("88")
                    answer_review.get_by_label("I confirm this answer is accurate and may be reused", exact=True).check()
                    answer_review.get_by_role("textbox", name="Answer", exact=True).fill(
                        "A revised synthetic answer requires a fresh review and reuse confirmation.")
                    expect(answer_review.get_by_label("I reviewed this exact answer for this job")).not_to_be_checked()
                    expect(answer_review.get_by_label("I confirm this answer is accurate and may be reused", exact=True)).not_to_be_checked()
                    expect(answer_review.get_by_role("button", name="Confirm and save to Answer Bank")).to_be_disabled()
                    answer_review.get_by_role("textbox", name="Answer", exact=True).fill(
                        "In a synthetic lab, I managed Azure subscriptions and reviewed access scopes.")
                    answer_review.get_by_label("I reviewed this exact answer for this job").check()
                    answer_review.get_by_label("I confirm this answer is accurate and may be reused", exact=True).check()

                    # Switching to another saved answer must discard prior text-specific reuse consent.
                    answer_review.get_by_role("button", name="Use saved answer (review required)").nth(1).click()
                    expect(answer_review.get_by_label("I reviewed this exact answer for this job")).not_to_be_checked()
                    expect(answer_review.get_by_role("button", name="Confirm and save to Answer Bank", exact=True)).to_have_count(0)
                    expect(answer_review.get_by_role("button", name="Save to Answer Bank", exact=True)).to_be_disabled()
                    answer_review.get_by_label("I reviewed this exact answer for this job").check()
                    answer_review.get_by_role("button", name="Save to Answer Bank", exact=True).click()
                    expect(answer_review.get_by_label("Answer bank confidence (%)", exact=True)).to_have_value("80")
                    expect(answer_review.get_by_label("I confirm this answer is accurate and may be reused", exact=True)).not_to_be_checked()
                    expect(answer_review.get_by_role("button", name="Confirm and save to Answer Bank")).to_be_disabled()
                    answer_review.get_by_label("Answer bank confidence (%)", exact=True).fill("88")
                    answer_review.get_by_label("I confirm this answer is accurate and may be reused", exact=True).check()

                    # Simulate a concurrent profile edit after the UI's expected version is read.
                    conflict = {"armed": True}

                    def concurrent_edit(route):
                        if conflict["armed"] and route.request.method == "PUT":
                            conflict["armed"] = False
                            current = store.profile("fixture")
                            store.save_profile({**current, "answers": [*current["answers"], {
                                "id": "concurrent-answer", "question": "A separate synthetic question?",
                                "text": "Concurrent edit", "source": "Other local edit",
                                "confidence": 90, "verified": True, "user_approved": True,
                            }]})
                        route.continue_()

                    context.route("**/profiles", concurrent_edit)
                    answer_review.get_by_role("button", name="Confirm and save to Answer Bank").click()
                    expect(page.locator('main > .alert[role="alert"]')).to_contain_text(
                        "Profile changed while this answer was being saved")
                    self.assertTrue(any(item["id"] == "concurrent-answer"
                                        for item in store.profile("fixture")["answers"]))
                    self.assertFalse(any(item["id"] == "azure-answer" and item["confidence"] == 88
                                         for item in store.profile("fixture")["answers"]))
                    context.unroute("**/profiles", concurrent_edit)

                    # Retry from the current version, then verify durable profile and per-job export.
                    answer_review.get_by_role("button", name="Confirm and save to Answer Bank").click()
                    expect(page.get_by_role("status").filter(has_text="saved to this profile's Answer Bank")).to_be_visible()
                    saved = store.profile("fixture")
                    azure = next(item for item in saved["answers"] if item.get("question") == QUESTION)
                    self.assertEqual(azure["confidence"], 88)
                    self.assertTrue(azure["verified"] and azure["user_approved"])
                    self.assertEqual(azure["text"], "An alternate synthetic answer from the profile bank.")
                    self.assertTrue(any(item["id"] == "concurrent-answer" for item in saved["answers"]))

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    page.get_by_role("region", name="Not tracked", exact=True).get_by_role(
                        "button", name=job["title"], exact=False).click()
                    expect(page.get_by_role("region", name="ATS question preflight")).to_be_visible()
                    page.get_by_role("button", name="Review Queue", exact=False).click()
                    persisted_queue = page.get_by_role("region", name="Application review queue")
                    expect(persisted_queue).to_contain_text("UNRESOLVED_REQUIRED_QUESTIONS")
                    expect(persisted_queue).to_contain_text(QUESTION)
                    persisted_queue.get_by_role("button", name=f"Review application: {job['title']} at {job['company']}").click()
                    page.get_by_role("button", name="Review this question").first.click()
                    expect(page.get_by_text("saved-answer confidence 88%", exact=False)).to_be_visible()
                    expect(page.get_by_text("Provenance: Reviewed for Example Systems Inc.", exact=False)).to_be_visible()

                    # Sensitive/legal answers are application-only and never offered for reuse.
                    page.get_by_role("button", name="Review this question").last.click()
                    sensitive = page.get_by_role("group", name="Question 2")
                    expect(page.get_by_label("Employer question").last).to_have_value(SENSITIVE_QUESTION)
                    expect(sensitive).to_contain_text("Sensitive or legal question")
                    expect(sensitive.get_by_role("button", name="Save to Answer Bank", exact=True)).to_have_count(0)
                    azure_review = page.get_by_role("group", name="Question 1")
                    azure_review.get_by_role("button", name="Use saved answer (review required)").first.click()
                    azure_review.get_by_label("I reviewed this exact answer for this job").check()
                    sensitive.get_by_role("textbox", name="Answer", exact=True).fill(
                        "Synthetic application-only response, provided explicitly by the user.")
                    sensitive.get_by_label("I reviewed this exact answer for this job").check()

                    # Review and export are explicitly per-application; there is no submit action here.
                    with page.expect_response(lambda response: response.url.endswith("/answers/export")) as exported:
                        page.get_by_role("button", name="Save reviewed answers and export TXT").click()
                    self.assertEqual(exported.value.status, 201)
                    self.assertEqual(exported.value.json()["external_submission"], "OFF")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    page.get_by_role("region", name="Not tracked", exact=True).get_by_role(
                        "button", name=job["title"], exact=False).click()
                    export_document = page.get_by_role("region", name="application-answer-export version 1")
                    expect(export_document.get_by_text("Manually reviewed application answers", exact=False)).to_be_visible()
                    self.assertEqual(store.count("documents"), 3)
                    self.assertEqual(store.application(application_id)["state"], application_before["state"])
                    self.assertEqual(store.application(application_id)["confirmation"], application_before["confirmation"])
                    self.assertEqual(store.count("application_attempts"), 0)
                    page.get_by_label("Active profile").select_option("other-profile")
                    page.get_by_role("button", name="Review Queue", exact=False).click()
                    isolated_queue = page.get_by_role("region", name="Application review queue")
                    expect(isolated_queue).to_contain_text("No saved applications currently need question review.")
                    expect(isolated_queue).not_to_contain_text(job["company"])
                    self.assertFalse(outside)
                    self.assertFalse(errors, errors)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
