"""Connected review-flow acceptance; requires ROOT to mount the match API and picker."""
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
                     "Build apps/web after ROOT mounts answer matching")
class ApplicationAnswerMatchingReviewFlowBrowserTests(unittest.TestCase):
    def test_scoped_lookup_selection_and_application_review_flow(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-answer-review-flow-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            answers = [
                {
                    "id": "exact-experience",
                    "question": "How many years of professional experience have you had?",
                    "text": "I have five years of synthetic professional experience.",
                    "source": "Synthetic user-authored exact evidence",
                    "verified": True,
                    "user_approved": True,
                    "confidence": 95,
                },
                {
                    "id": "fuzzy-experience",
                    "question": "How many years of professional experience do you have?",
                    "text": "Synthetic answer for a fuzzy wording match.",
                    "source": "Synthetic user-authored fuzzy evidence",
                    "verified": True,
                    "user_approved": True,
                    "confidence": 94,
                },
                {
                    "id": "sensitive-authorization",
                    "question": "Are you legally authorized for employment in the United States?",
                    "text": "Synthetic legal answer that matching must not reuse.",
                    "source": "Synthetic sensitive fixture",
                    "verified": True,
                    "user_approved": True,
                    "confidence": 100,
                },
            ]
            store.save_profile(dict(PROFILE, answers=answers))
            store.save_profile(dict(PROFILE, id="other", name="Other synthetic profile",
                                    email="other@example.test", answers=[]))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")

            jobs = store.jobs("fixture")
            self.assertGreaterEqual(len(jobs), 2, "fixture should provide two distinct applications")
            first_job, second_job = jobs[:2]
            first_application = prepare(store, first_job["id"], "fixture", root)
            second_application = prepare(store, second_job["id"], "fixture", root)
            other_application = prepare(store, first_job["id"], "other", root)
            initial_fixture = store.profile("fixture")
            initial_other = store.profile("other")
            initial_documents = store.count("documents")

            # Exercise the mounted local router and browser picker against a
            # disposable fixture store; no external service is contacted.
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            origin_netloc = urlsplit(origin).netloc
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            outside, submission_requests, all_requests, matching_requests, page_errors = (
                [], [], [], [], [])

            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated answer review API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script(
                        "localStorage.setItem('mjobs-profile', 'fixture')")

                    def local_only(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        all_requests.append((request.method, parsed.path))
                        if parsed.netloc != origin_netloc:
                            outside.append(request.url)
                            route.abort()
                            return
                        if parsed.path.endswith("/answers/match"):
                            matching_requests.append({
                                "method": request.method,
                                "path": parsed.path,
                                "body": request.post_data_json,
                            })
                        if request.method != "GET" and any(
                                marker in parsed.path.lower()
                                for marker in ("/submit", "/apply", "/mock-run")):
                            submission_requests.append((request.method, parsed.path))
                            route.abort()
                            return
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_role("navigation", name="Main navigation").get_by_role("button", name="Applications", exact=False).click()
                    first_card = page.locator(".job-card").filter(
                        has_text=first_job["title"])
                    first_card.click()
                    answers_panel = page.get_by_role(
                        "region", name="Application answers")
                    answers_panel.get_by_role(
                        "button", name="Add question", exact=True).click()
                    question_group = answers_panel.get_by_role("group", name="Question 1")
                    question = "How many years of professional experience have you had?"
                    question_group.get_by_label("Employer question", exact=True).fill(question)
                    answer = question_group.get_by_role("textbox", name="Answer", exact=True)
                    lookup = question_group.get_by_role(
                        "button", name="Find saved answer matches", exact=True)
                    candidates = question_group.get_by_role(
                        "list", name="Saved answer match candidates")

                    # Question entry does not run lookup or copy a candidate.
                    expect(answer).to_have_value("")
                    expect(candidates).to_have_count(0)
                    self.assertEqual(matching_requests, [])
                    with page.expect_response(lambda response:
                            response.url.endswith(
                                f"/profiles/fixture/applications/{first_application}/answers/match")) as matched:
                        lookup.click()
                    self.assertEqual(matched.value.status, 200, matched.value.text())
                    expect(candidates).to_be_visible()
                    self.assertEqual(len(matching_requests), 1, matching_requests)
                    self.assertEqual(matching_requests[0]["method"], "POST")
                    self.assertEqual(
                        matching_requests[0]["path"],
                        f"/profiles/fixture/applications/{first_application}/answers/match")
                    self.assertEqual(matching_requests[0]["body"], {"question": question})

                    exact = candidates.get_by_role("listitem", name="Answer match 1")
                    expect(exact).to_contain_text("Exact normalized match")
                    expect(exact).to_contain_text("match score 100%")
                    expect(exact).to_contain_text(question)
                    expect(exact).to_contain_text("Synthetic user-authored exact evidence")
                    expect(exact).to_contain_text(
                        "I have five years of synthetic professional experience.")
                    fuzzy = candidates.get_by_role("listitem", name="Answer match 2")
                    expect(fuzzy).to_contain_text("Fuzzy match")
                    expect(fuzzy).to_contain_text("match score 78%")
                    expect(fuzzy).to_contain_text("Synthetic user-authored fuzzy evidence")
                    review = question_group.get_by_label(
                        "I reviewed this exact answer for this job", exact=True)
                    export = answers_panel.get_by_role(
                        "button", name="Save reviewed answers and export TXT", exact=True)
                    expect(answer).to_have_value("")
                    expect(review).not_to_be_checked()
                    expect(export).to_be_disabled()
                    self.assertEqual(store.profile("fixture")["answers"],
                                     initial_fixture["answers"])
                    self.assertEqual(store.profile("fixture")["version"],
                                     initial_fixture["version"])
                    self.assertEqual(store.count("documents"), initial_documents)
                    self.assertFalse([(method, path) for method, path in all_requests
                                      if method not in {"GET", "HEAD"}
                                      and not path.endswith("/answers/match")], all_requests)

                    exact.get_by_role(
                        "button", name=("Select answer as draft: "
                                        "I have five years of synthetic professional experience.")).click()
                    expect(answer).to_have_value(
                        "I have five years of synthetic professional experience.")
                    expect(review).not_to_be_checked()
                    expect(export).to_be_disabled()
                    self.assertEqual(store.profile("fixture")["answers"],
                                     initial_fixture["answers"])
                    self.assertEqual(store.count("documents"), initial_documents)

                    # Editing the question makes the old response unusable.
                    changed_question = "Describe a fictional dessert you like."
                    question_group.locator("#application-answer-question-0").fill(
                        changed_question)
                    expect(candidates.get_by_role("listitem", name="Answer match 1")).to_have_count(0)
                    expect(question_group.get_by_role(
                        "button", name="Select answer as draft:", exact=False)).to_have_count(0)
                    expect(answer).to_have_value("")
                    self.assertFalse(any(
                        item["body"] == {"question": changed_question}
                        for item in matching_requests))
                    with page.expect_response(lambda response:
                            response.url.endswith(
                                f"/profiles/fixture/applications/{first_application}/answers/match")) as changed_match:
                        lookup.click()
                    self.assertEqual(changed_match.value.status, 200, changed_match.value.text())
                    expect(question_group.get_by_role(
                        "region", name="Saved answer matches")).to_contain_text(
                            "No eligible saved answer matches were found.")
                    self.assertEqual(matching_requests[-1]["body"],
                                     {"question": changed_question})

                    # A different application invalidates the first app's
                    # response; an explicit lookup carries its own app id.
                    second_card = page.locator(".job-card").filter(
                        has_text=second_job["title"])
                    second_card.click()
                    expect(page.get_by_role(
                        "region", name="Application review", exact=True)).to_contain_text(
                            second_job["title"])
                    second_panel = page.get_by_role(
                        "region", name="Application answers")
                    second_panel.get_by_role(
                        "button", name="Add question", exact=True).click()
                    second_group = second_panel.get_by_role("group", name="Question 1")
                    second_group.get_by_label("Employer question", exact=True).fill(question)
                    second_answer = second_group.get_by_role(
                        "textbox", name="Answer", exact=True)
                    expect(second_answer).to_have_value("")
                    second_match_panel = second_group.get_by_role(
                        "region", name="Saved answer matches")
                    expect(second_match_panel.get_by_role(
                        "list", name="Saved answer match candidates")).to_have_count(0)
                    with page.expect_response(lambda response:
                            response.url.endswith(
                                f"/profiles/fixture/applications/{second_application}/answers/match")) as second_match:
                        second_match_panel.get_by_role(
                            "button", name="Find saved answer matches", exact=True).click()
                    self.assertEqual(second_match.value.status, 200, second_match.value.text())
                    self.assertEqual(matching_requests[-1]["path"],
                                     f"/profiles/fixture/applications/{second_application}/answers/match")
                    expect(second_match_panel.get_by_role(
                        "list", name="Saved answer match candidates")).to_be_visible()

                    # Profile switch also invalidates prior candidates and the
                    # backend enforces profile/application ownership.
                    page.get_by_label("Active profile", exact=True).select_option("other")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("other")
                    expect(page.get_by_role(
                        "button", name="Select answer as draft:", exact=False)).to_have_count(0)
                    other_card = page.locator(".job-card").filter(
                        has_text=first_job["title"])
                    with page.expect_response(lambda response:
                            response.request.method == "GET" and response.url.endswith(
                                f"/profiles/other/applications/{other_application}")) as other_detail:
                        other_card.click()
                    self.assertEqual(other_detail.value.status, 200, other_detail.value.text())
                    other_panel = page.get_by_role(
                        "region", name="Application answers")
                    other_panel.get_by_role(
                        "button", name="Add question", exact=True).click()
                    other_group = other_panel.get_by_role("group", name="Question 1")
                    other_group.get_by_label("Employer question", exact=True).fill(question)
                    other_answer = other_group.get_by_role(
                        "textbox", name="Answer", exact=True)
                    expect(other_answer).to_have_value("")
                    other_match_panel = other_group.get_by_role(
                        "region", name="Saved answer matches")
                    expect(other_match_panel.get_by_role(
                        "list", name="Saved answer match candidates")).to_have_count(0)
                    with page.expect_response(lambda response:
                            response.url.endswith(
                                f"/profiles/other/applications/{other_application}/answers/match")) as other_match:
                        other_match_panel.get_by_role(
                            "button", name="Find saved answer matches", exact=True).click()
                    self.assertEqual(other_match.value.status, 200, other_match.value.text())
                    self.assertEqual(matching_requests[-1]["body"], {"question": question})
                    expect(other_match_panel).to_contain_text(
                        "No eligible saved answer matches")
                    self.assertNotIn("exact-experience", other_match.value.text())

                    # Return to the fixture profile/app and deliberately
                    # select/review before permitting a durable local export.
                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    first_card = page.locator(".job-card").filter(
                        has_text=first_job["title"])
                    with page.expect_response(lambda response:
                            response.request.method == "GET" and response.url.endswith(
                                f"/profiles/fixture/applications/{first_application}")) as first_detail:
                        first_card.click()
                    self.assertEqual(first_detail.value.status, 200, first_detail.value.text())
                    export_panel = page.get_by_role(
                        "region", name="Application answers")
                    export_panel.get_by_role(
                        "button", name="Add question", exact=True).click()
                    export_group = export_panel.get_by_role("group", name="Question 1")
                    export_group.get_by_label("Employer question", exact=True).fill(question)
                    export_matches = export_group.get_by_role(
                        "region", name="Saved answer matches")
                    with page.expect_response(lambda response:
                            response.url.endswith(
                                f"/profiles/fixture/applications/{first_application}/answers/match")) as final_match:
                        export_matches.get_by_role(
                            "button", name="Find saved answer matches", exact=True).click()
                    self.assertEqual(final_match.value.status, 200, final_match.value.text())
                    export_matches.get_by_role(
                        "listitem", name="Answer match 1").get_by_role(
                            "button", name=("Select answer as draft: "
                                            "I have five years of synthetic professional experience.")).click()
                    export_answer = export_group.get_by_role(
                        "textbox", name="Answer", exact=True)
                    export_review = export_group.get_by_label(
                        "I reviewed this exact answer for this job", exact=True)
                    export_button = export_panel.get_by_role(
                        "button", name="Save reviewed answers and export TXT", exact=True)
                    expect(export_answer).to_have_value(
                        "I have five years of synthetic professional experience.")
                    expect(export_review).not_to_be_checked()
                    expect(export_button).to_be_disabled()
                    export_review.check()
                    expect(export_button).to_be_enabled()
                    with page.expect_response(lambda response:
                            response.url.endswith(
                                f"/profiles/fixture/applications/{first_application}/answers/export")) as saved:
                        export_button.click()
                    self.assertEqual(saved.value.status, 201, saved.value.text())
                    self.assertEqual(saved.value.json()["external_submission"], "OFF")
                    self.assertEqual(store.count("documents"), initial_documents + 1)
                    self.assertEqual(store.profile("fixture")["answers"],
                                     initial_fixture["answers"])
                    self.assertEqual(store.profile("other")["answers"],
                                     initial_other["answers"])

                    # Sensitive/legal prompts get no reusable candidates and
                    # never send their question to the matching route.
                    export_panel.get_by_role(
                        "button", name="Add question", exact=True).click()
                    sensitive_group = export_panel.get_by_role("group", name="Question 1")
                    sensitive_question = (
                        "Are you legally authorized to work in the United States?")
                    sensitive_group.locator("#application-answer-question-0").fill(
                        sensitive_question)
                    previous_match_count = len(matching_requests)
                    expect(sensitive_group).to_contain_text(
                        "Sensitive or legal question: enter your answer for this application.")
                    expect(sensitive_group.get_by_role(
                        "region", name="Saved answer matches")).to_have_count(0)
                    expect(sensitive_group.get_by_role(
                        "textbox", name="Answer", exact=True)).to_have_value("")
                    self.assertEqual(len(matching_requests), previous_match_count)

                    self.assertEqual(outside, [])
                    self.assertEqual(submission_requests, [])
                    self.assertFalse(page_errors, page_errors)
                    unexpected_mutations = [
                        (method, path) for method, path in all_requests
                        if method not in {"GET", "HEAD"}
                        and not path.endswith("/answers/match")
                        and not path.endswith("/answers/export")
                        and not path.endswith("/sensitive-answer-suggestion")
                    ]
                    self.assertEqual(unexpected_mutations, [], all_requests)
                    self.assertEqual(
                        [(method, path) for method, path in all_requests
                         if path.endswith("/answers/export")],
                        [("POST", f"/profiles/fixture/applications/{first_application}/answers/export")],
                        all_requests)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated answer review API must stop")


if __name__ == "__main__":
    unittest.main()
