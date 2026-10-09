"""Connected Chromium acceptance for scoped, review-gated answer matching."""
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


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ConnectedApplicationAnswerMatchingBrowserTests(unittest.TestCase):
    def test_ranked_candidates_are_scoped_read_only_and_review_gated(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-answer-matching-connected-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile_answers = [
                {
                    "id": "cloud-exact",
                    "question": "Which cloud platforms have you used?",
                    "text": "Synthetic exact candidate answer.",
                    "source": "Synthetic exact profile evidence",
                    "verified": True,
                    "user_approved": True,
                    "confidence": 96,
                },
                {
                    "id": "cloud-semantic",
                    "question": "What cloud providers have you used?",
                    "text": "Synthetic semantic candidate answer.",
                    "source": "Synthetic semantic profile evidence",
                    "verified": True,
                    "user_approved": True,
                    "confidence": 94,
                },
                {
                    "id": "cloud-fuzzy",
                    "question": "Which public cloud platforms have you used?",
                    "text": "Synthetic fuzzy candidate answer.",
                    "source": "Synthetic fuzzy profile evidence",
                    "verified": True,
                    "user_approved": True,
                    "confidence": 92,
                },
            ]
            store.save_profile(dict(PROFILE, answers=profile_answers))
            store.save_profile(dict(PROFILE, id="other", name="Other synthetic profile",
                                    email="other@example.test", answers=[]))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")

            jobs = store.jobs("fixture")
            self.assertGreaterEqual(len(jobs), 2, "fixture should provide two distinct applications")
            first_job, second_job = jobs[:2]
            first_application = prepare(store, first_job["id"], "fixture", root)
            second_application = prepare(store, second_job["id"], "fixture", root)
            prepare(store, first_job["id"], "other", root)
            initial_profile = store.profile("fixture")
            initial_other = store.profile("other")
            initial_documents = store.count("documents")

            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            origin_netloc = urlsplit(origin).netloc
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            outside, submissions, requests, page_errors = [], [], [], []
            failed_reads = set()

            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated answer-matching API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script(
                        "localStorage.setItem('mjobs-profile', 'fixture')")

                    def local_only(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        requests.append((request.method, parsed.path))
                        if parsed.netloc != origin_netloc:
                            outside.append(request.url)
                            route.abort()
                            return
                        if (request.method == "GET" and
                                parsed.path.endswith(
                                    f"/applications/{first_application}/answers") and
                                first_application not in failed_reads):
                            failed_reads.add(first_application)
                            route.fulfill(status=503,
                                          body='{"detail":"synthetic answer-bank outage"}',
                                          content_type="application/json")
                            return
                        if request.method != "GET" and any(
                                marker in parsed.path.lower()
                                for marker in ("/submit", "/apply", "/mock-run")):
                            submissions.append((request.method, parsed.path))
                            route.abort()
                            return
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()

                    first_card = page.locator(".job-card").filter(
                        has_text=first_job["title"])
                    first_card.click()
                    expect(page.get_by_role("alert")).to_be_visible()
                    self.assertIn(first_application, failed_reads)
                    first_card.click()  # An explicit user retry recovers the read.
                    first_answers = page.get_by_role("region", name="Application answers")
                    first_answers.get_by_role(
                        "button", name="Add question", exact=True).click()
                    first_question = first_answers.get_by_role("group", name="Question 1")
                    first_question.get_by_label("Employer question", exact=True).fill(
                        "Which cloud platforms have you used?")

                    match_rows = [text for text in first_question.locator("p").all_text_contents()
                                  if "match score" in text]
                    self.assertEqual(len(match_rows), 3, match_rows)
                    self.assertIn("Exact normalized match · match score 100%", match_rows[0])
                    self.assertIn("Curated semantic synonym match (local rules) · match score 95%",
                                  match_rows[1])
                    self.assertIn("Fuzzy wording match · match score 86%", match_rows[2])
                    expect(first_question).to_contain_text("Synthetic exact profile evidence")
                    expect(first_question).to_contain_text("Synthetic semantic profile evidence")
                    expect(first_question).to_contain_text("Synthetic fuzzy profile evidence")

                    first_answer = first_question.get_by_role(
                        "textbox", name="Answer", exact=True)
                    first_export = first_answers.get_by_role(
                        "button", name="Save reviewed answers and export TXT", exact=True)
                    first_review = first_question.get_by_label(
                        "I reviewed this exact answer for this job", exact=True)
                    expect(first_answer).to_have_value("")
                    expect(first_review).not_to_be_checked()
                    expect(first_export).to_be_disabled()
                    self.assertEqual(store.profile("fixture")["answers"],
                                     initial_profile["answers"])
                    self.assertEqual(store.count("documents"), initial_documents)
                    self.assertEqual([(method, path) for method, path in requests
                                      if method not in {"GET", "HEAD"}], [], requests)

                    # Selection is explicit, changes only the local draft, and
                    # leaves per-application approval false.
                    first_question.get_by_role(
                        "button", name="Use saved answer (review required)", exact=True).nth(1).click()
                    expect(first_answer).to_have_value("Synthetic semantic candidate answer.")
                    expect(first_review).not_to_be_checked()
                    expect(first_export).to_be_disabled()

                    # Drafts and approval cannot leak into another application.
                    second_card = page.locator(".job-card").filter(
                        has_text=second_job["title"])
                    second_card.click()
                    expect(page.get_by_role(
                        "region", name="Application review")).to_contain_text(
                            second_job["title"])
                    second_answers = page.get_by_role("region", name="Application answers")
                    second_answers.get_by_role(
                        "button", name="Add question", exact=True).click()
                    second_question = second_answers.get_by_role("group", name="Question 1")
                    second_answer = second_question.get_by_role(
                        "textbox", name="Answer", exact=True)
                    second_review = second_question.get_by_label(
                        "I reviewed this exact answer for this job", exact=True)
                    expect(second_answer).to_have_value("")
                    expect(second_review).not_to_be_checked()

                    # A different profile cannot see the fixture profile's
                    # answer candidates, even for the same prepared job.
                    page.get_by_label("Active profile", exact=True).select_option("other")
                    other_card = page.locator(".job-card").filter(
                        has_text=first_job["title"])
                    other_card.click()
                    other_answers = page.get_by_role("region", name="Application answers")
                    other_answers.get_by_role(
                        "button", name="Add question", exact=True).click()
                    other_question = other_answers.get_by_role("group", name="Question 1")
                    other_question.get_by_label("Employer question", exact=True).fill(
                        "Which cloud platforms have you used?")
                    expect(other_question.get_by_role(
                        "button", name="Use saved answer (review required)", exact=True)).to_have_count(0)
                    expect(other_question.get_by_role(
                        "textbox", name="Answer", exact=True)).to_have_value("")

                    # Return to the owning profile: the exact answer still
                    # requires fresh user review before the local export path.
                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    first_card = page.locator(".job-card").filter(
                        has_text=first_job["title"])
                    first_card.click()
                    export_answers = page.get_by_role(
                        "region", name="Application answers")
                    export_answers.get_by_role(
                        "button", name="Add question", exact=True).click()
                    export_question = export_answers.get_by_role("group", name="Question 1")
                    export_question.get_by_label("Employer question", exact=True).fill(
                        "Which cloud platforms have you used?")
                    export_question.get_by_role(
                        "button", name="Use saved answer (review required)", exact=True).first.click()
                    export_answer = export_question.get_by_role(
                        "textbox", name="Answer", exact=True)
                    export_review = export_question.get_by_label(
                        "I reviewed this exact answer for this job", exact=True)
                    export_button = export_answers.get_by_role(
                        "button", name="Save reviewed answers and export TXT", exact=True)
                    expect(export_answer).to_have_value("Synthetic exact candidate answer.")
                    expect(export_review).not_to_be_checked()
                    expect(export_button).to_be_disabled()
                    export_review.check()
                    expect(export_button).to_be_enabled()
                    with page.expect_response(lambda response:
                            response.url.endswith(
                                f"/applications/{first_application}/answers/export")) as saved:
                        export_button.click()
                    self.assertEqual(saved.value.status, 201, saved.value.text)
                    self.assertEqual(saved.value.json()["external_submission"], "OFF")
                    self.assertEqual(store.count("documents"), initial_documents + 1)
                    self.assertEqual(store.profile("fixture")["answers"],
                                     initial_profile["answers"])
                    self.assertEqual(store.profile("fixture")["version"],
                                     initial_profile["version"])
                    self.assertEqual(store.profile("other")["answers"],
                                     initial_other["answers"])
                    self.assertEqual(store.profile("other")["version"],
                                     initial_other["version"])
                    self.assertEqual(submissions, [])
                    self.assertEqual(outside, [])
                    self.assertFalse(page_errors, page_errors)
                    self.assertEqual(
                        [(method, path) for method, path in requests
                         if method not in {"GET", "HEAD"}],
                        [("POST", f"/profiles/fixture/applications/{first_application}/answers/export")],
                        requests)
                    self.assertNotEqual(first_application, second_application)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated answer-matching server must stop")


if __name__ == "__main__":
    unittest.main()
