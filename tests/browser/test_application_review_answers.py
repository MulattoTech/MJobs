"""Connected local-browser acceptance for manually reviewed application answers."""
from copy import deepcopy
import json
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
from services.applications.preflight import persist_preflight
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
QUESTION_ID = "manual-azure-review"
QUESTION = "Describe your experience managing Azure subscriptions."
SENSITIVE_QUESTION_ID = "legal-authorization-review"
SENSITIVE_QUESTION = "Are you legally authorized to work in the United States?"
MANUAL_ANSWER = (
    "In a synthetic lab, I managed Azure subscriptions and reviewed access scopes."
)
CONCURRENT_ANSWER_ID = "concurrent-version-change"


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationReviewAnswersBrowserTests(unittest.TestCase):
    def test_reviewed_answer_requires_persisted_save_and_isolated_readback(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-reviewed-answer-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = deepcopy(PROFILE)
            profile["answers"] = []
            store.save_profile(profile)
            other_profile = deepcopy(PROFILE)
            other_profile.update(id="other-profile", name="Other synthetic applicant",
                                 email="other@example.invalid", answers=[])
            store.save_profile(other_profile)

            # The fixture server is loopback-only and serves synthetic postings; the
            # browser below blocks every origin other than its isolated local API.
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
                discover(store, fixture, "other-profile")
            job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
            other_job = next(row for row in store.jobs("other-profile")
                             if row["requisition"] == "B")
            self.assertEqual(job["id"], other_job["id"])
            application_ids = {
                "fixture": prepare(store, job["id"], "fixture", root),
                "other-profile": prepare(store, other_job["id"], "other-profile", root),
            }
            for profile_id, application_id in application_ids.items():
                application = store.application(application_id)
                persist_preflight(store, application_id, {
                    "result": "FAIL",
                    "reason": "UNRESOLVED_REQUIRED_QUESTIONS",
                    "observed_at": "2026-10-09T00:00:00Z",
                    "profile_version": application["profile_version"],
                    "document_version": application["document_version"],
                    "route_id": application["route_id"],
                    "provider": "mock-ats",
                    "tenant": "example",
                    "requisition": "B",
                    "metadata_fingerprint": "b" * 64,
                    "mappings": [],
                    "missing": [
                        {"question_id": QUESTION_ID, "question": QUESTION,
                         "fact_ids": [], "answer_ids": [], "profile_paths": [],
                         "document_kinds": []},
                        {"question_id": SENSITIVE_QUESTION_ID,
                         "question": SENSITIVE_QUESTION,
                         "fact_ids": [], "answer_ids": [], "profile_paths": [],
                         "document_kinds": []},
                    ],
                })

            applications_before = [store.application(identifier)
                                   for identifier in application_ids.values()]
            documents_before = store.count("documents")
            attempts_before = store.count("application_attempts")
            app = create_app(store, fixture, root, operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                app, log_level="error", access_log=False))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            external, errors, writes = [], [], []
            request_bodies = []
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated application review API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1365, "height": 950})

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def record_write(request):
                        if request.method not in {"GET", "HEAD", "OPTIONS"}:
                            writes.append((request.method, urlsplit(request.url).path))
                            if request.post_data:
                                try:
                                    request_bodies.append(json.loads(request.post_data))
                                except (TypeError, json.JSONDecodeError):
                                    pass

                    page.on("request", record_write)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value(
                        "fixture")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name="Review Queue", exact=False).click()
                    queue = page.get_by_role("region", name="Application review queue")
                    expect(queue).to_contain_text(job["company"])
                    queue.get_by_role("button", name=(
                        f"Review application: {job['title']} at {job['company']}"
                    )).click()

                    preflight = page.get_by_role("region", name="ATS question preflight")
                    unresolved = preflight.get_by_role(
                        "list", name="Unresolved required questions")
                    question_row = unresolved.locator("li").filter(has_text=QUESTION)
                    question_row.get_by_role(
                        "button", name="Review this question", exact=True).click()
                    editor = page.get_by_role(
                        "region", name="Application answers", exact=True).get_by_role(
                            "group", name="Question 1")
                    expect(editor.get_by_role("textbox", name="Employer question", exact=True)).to_have_value(
                        QUESTION)
                    answer = editor.get_by_role("textbox", name="Answer", exact=True)
                    expect(answer).to_have_value("")
                    expect(editor).not_to_contain_text("Suggested answer")
                    expect(editor).not_to_contain_text("Confidence")
                    bank_button = editor.get_by_role(
                        "button", name="Save to Answer Bank", exact=True)
                    expect(bank_button).to_be_disabled()

                    # Editing and reviewing text remains local until the explicit save.
                    answer.fill(MANUAL_ANSWER)
                    editor.get_by_label("I reviewed this exact answer for this job", exact=True).check()
                    self.assertEqual(writes, [],
                                     "Typing and review acknowledgement must not persist")
                    self.assertFalse(any(item.get("text") == MANUAL_ANSWER
                                         for item in store.profile("fixture")["answers"]))
                    expect(bank_button).to_be_enabled()
                    bank_button.click()
                    confirmation = editor.locator('[aria-label="Answer Bank confirmation for question 1"]')
                    confirm_save = confirmation.get_by_role(
                        "button", name="Confirm and save to Answer Bank", exact=True)
                    expect(confirm_save).to_be_disabled()
                    confirmation.get_by_label(
                        "I confirm this answer is accurate and may be reused", exact=True).check()
                    expect(confirm_save).to_be_enabled()
                    self.assertEqual(writes, [], "Confirmation is local until explicit save")

                    # Change the profile after the save reads its expected version.
                    # The first save must be rejected without persisting this answer.
                    def concurrent_profile_change(route):
                        if (route.request.method == "PUT"
                                and urlsplit(route.request.url).path == "/profiles"):
                            current = store.profile("fixture")
                            store.save_profile({**current, "answers": [
                                *current["answers"], {
                                    "id": CONCURRENT_ANSWER_ID,
                                    "question": "A concurrent synthetic question?",
                                    "text": "Unrelated concurrent profile edit.",
                                    "source": "Synthetic concurrent edit",
                                    "confidence": 90,
                                    "verified": True,
                                    "user_approved": True,
                                },
                            ]})
                        route.continue_()

                    context.route("**/profiles", concurrent_profile_change)
                    with page.expect_response(lambda response:
                            response.request.method == "PUT"
                            and urlsplit(response.url).path == "/profiles") as stale_save:
                        confirm_save.click()
                    self.assertEqual(stale_save.value.status, 409)
                    expect(page.locator('main > .alert[role="alert"]')).to_contain_text(
                        "Profile changed while this answer was being saved")
                    self.assertFalse(any(item.get("text") == MANUAL_ANSWER
                                         for item in store.profile("fixture")["answers"]))
                    expect(confirmation).to_be_visible()
                    context.unroute("**/profiles", concurrent_profile_change)

                    # Reload obtains the current profile version. Saving now must
                    # return the persisted text before reporting a successful bank save.
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name="Review Queue", exact=False).click()
                    queue = page.get_by_role("region", name="Application review queue")
                    queue.get_by_role("button", name=(
                        f"Review application: {job['title']} at {job['company']}"
                    )).click()
                    preflight = page.get_by_role("region", name="ATS question preflight")
                    unresolved = preflight.get_by_role(
                        "list", name="Unresolved required questions")
                    question_row = unresolved.locator("li").filter(has_text=QUESTION)
                    question_row.get_by_role(
                        "button", name="Review this question", exact=True).click()
                    editor = page.get_by_role(
                        "region", name="Application answers", exact=True).get_by_role(
                            "group", name="Question 1")
                    answer = editor.get_by_role("textbox", name="Answer", exact=True)
                    answer.fill(MANUAL_ANSWER)
                    editor.get_by_label("I reviewed this exact answer for this job", exact=True).check()
                    editor.get_by_role("button", name="Save to Answer Bank", exact=True).click()
                    confirmation = editor.locator('[aria-label="Answer Bank confirmation for question 1"]')
                    confirm_save = confirmation.get_by_role(
                        "button", name="Confirm and save to Answer Bank", exact=True)
                    expect(confirm_save).to_be_disabled()
                    confirmation.get_by_label(
                        "I confirm this answer is accurate and may be reused", exact=True).check()
                    with page.expect_response(lambda response:
                            response.request.method == "PUT"
                            and urlsplit(response.url).path == "/profiles") as saved_response:
                        with page.expect_response(lambda response:
                                response.request.method == "GET"
                                and urlsplit(response.url).path == "/profiles/fixture"
                                and MANUAL_ANSWER in response.text()) as saved_readback:
                            confirm_save.click()
                    self.assertEqual(saved_response.value.status, 200)
                    payload = saved_response.value.json()
                    self.assertEqual(payload["id"], "fixture")
                    self.assertTrue(payload["version"])
                    saved_answer_profile = saved_readback.value.json()
                    self.assertEqual(saved_answer_profile["id"], "fixture")
                    persisted_response = next(item for item in saved_answer_profile["answers"]
                                              if item.get("question") == QUESTION)
                    self.assertEqual(persisted_response["text"], MANUAL_ANSWER)
                    self.assertTrue(persisted_response["verified"])
                    self.assertTrue(persisted_response["user_approved"])
                    saved_profile = store.profile("fixture")
                    persisted = next(item for item in saved_profile["answers"]
                                     if item.get("question") == QUESTION)
                    self.assertEqual(persisted["text"], MANUAL_ANSWER)
                    self.assertTrue(persisted["user_approved"])
                    self.assertTrue(persisted["verified"])
                    self.assertTrue(any(item.get("id") == CONCURRENT_ANSWER_ID
                                        for item in saved_profile["answers"]),
                                    "Retry must preserve the concurrent profile edit")
                    self.assertNotEqual(saved_profile["version"],
                                        applications_before[0]["profile_version"])
                    expect(page.get_by_role("status").filter(has_text="Reviewed answer saved")).to_contain_text(
                        "Reviewed answer saved to this profile's Answer Bank")
                    expect(confirm_save).to_be_disabled()
                    expect(editor).to_contain_text("Selected draft only")
                    self.assertEqual(len(writes), 2,
                                     "Readback and selection must not write application state")

                    # A browser reload followed by the profile-scoped answers read
                    # route proves that the exact user text survived the page lifecycle.
                    page.reload(wait_until="domcontentloaded")
                    readback = page.request.get(
                        f"{origin}/profiles/fixture/applications/"
                        f"{application_ids['fixture']}/answers")
                    self.assertEqual(readback.status, 200)
                    readback_answers = readback.json()["answers"]
                    self.assertTrue(any(
                        item.get("question") == QUESTION
                        and item.get("text") == MANUAL_ANSWER
                        for item in readback_answers), readback_answers)
                    settings = page.request.get(f"{origin}/settings")
                    self.assertFalse(settings.json()["external_submission_enabled"])

                    # A different profile sees no answer from profile A.
                    page.get_by_label("Active profile", exact=True).select_option(
                        "other-profile")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name="Review Queue", exact=False).click()
                    other_queue = page.get_by_role(
                        "region", name="Application review queue")
                    other_queue.get_by_role("button", name=(
                        f"Review application: {other_job['title']} at {other_job['company']}"
                    )).click()
                    preflight = page.get_by_role("region", name="ATS question preflight")
                    unresolved = preflight.get_by_role(
                        "list", name="Unresolved required questions")
                    question_row = unresolved.locator("li").filter(has_text=QUESTION)
                    question_row.get_by_role(
                        "button", name="Review this question", exact=True).click()
                    other_editor = page.get_by_role(
                        "region", name="Application answers", exact=True).get_by_role("group", name="Question 1")
                    expect(other_editor.get_by_role("textbox", name="Answer", exact=True)).to_have_value("")
                    self.assertFalse(any(item.get("text") == MANUAL_ANSWER
                                         for item in store.profile("other-profile")["answers"]))

                    # Sensitive prompts cannot be promoted into the reusable bank.
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name="Review Queue", exact=False).click()
                    other_queue = page.get_by_role(
                        "region", name="Application review queue")
                    other_queue.get_by_role("button", name=(
                        f"Review application: {other_job['title']} at {other_job['company']}"
                    )).click()
                    preflight = page.get_by_role("region", name="ATS question preflight")
                    unresolved = preflight.get_by_role(
                        "list", name="Unresolved required questions")
                    sensitive_row = unresolved.locator("li").filter(
                        has_text=SENSITIVE_QUESTION)
                    sensitive_row.get_by_role(
                        "button", name="Review this question", exact=True).click()
                    sensitive_editor = page.get_by_role(
                        "region", name="Application answers", exact=True).get_by_role("group", name="Question 1")
                    expect(sensitive_editor.get_by_role(
                        "textbox", name="Employer question", exact=True)).to_have_value(SENSITIVE_QUESTION)
                    expect(sensitive_editor).to_contain_text(
                        "Sensitive or legal question: enter your answer for this application.")
                    sensitive_editor.get_by_role("textbox", name="Answer", exact=True).fill("Synthetic application-only answer")
                    sensitive_editor.get_by_label(
                        "I reviewed this exact answer for this job", exact=True).check()
                    expect(sensitive_editor.get_by_role(
                        "button", name="Save to Answer Bank", exact=True)).to_have_count(0)
                    expect(sensitive_editor.get_by_role(
                        "button", name="Confirm and save to Answer Bank", exact=True)).to_have_count(0)

                    self.assertEqual(writes, [
                        ("PUT", "/profiles"),
                        ("PUT", "/profiles"),
                    ])
                    self.assertEqual(len(request_bodies), 2)
                    for profile_id in ("fixture", "other-profile"):
                        self.assertFalse(any(
                            item.get("text") == "Synthetic application-only answer"
                            for item in store.profile(profile_id)["answers"]))
                    self.assertTrue(any(item.get("id") == CONCURRENT_ANSWER_ID
                                        for item in request_bodies[-1]["answers"]))
                    for body in request_bodies:
                        self.assertEqual(body["id"], "fixture")
                        self.assertTrue(body["expected_version"])
                        self.assertFalse(any(
                            item.get("text") == "Synthetic application-only answer"
                            for item in body["answers"]))
                        self.assertTrue(any(
                            item.get("question") == QUESTION
                            and item.get("text") == MANUAL_ANSWER
                            for item in body["answers"]))
                    self.assertEqual(store.count("documents"), documents_before)
                    self.assertEqual(store.count("application_attempts"), attempts_before)
                    for before in applications_before:
                        self.assertEqual(store.application(before["id"]), before)
                    self.assertFalse(external, external)
                    self.assertFalse(errors, errors)
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated application review server must stop")


if __name__ == "__main__":
    unittest.main()
