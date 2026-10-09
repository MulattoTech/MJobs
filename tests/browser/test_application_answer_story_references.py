"""Connected synthetic acceptance for profile-scoped story citations in answer review."""
from copy import deepcopy
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
from services.career_profile.story_bank import create_story, review_story, revise_story
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
ANSWER_MATCH_ROUTE = "/applications/{application_id}/answers/match"
CURRENT_STORY_TEXT = "Maintained synthetic Linux services for four test sites."


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class ApplicationAnswerStoryReferenceBrowserTests(unittest.TestCase):
    def test_current_story_is_a_scoped_citation_with_separate_answer_review(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-answer-story-reference-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")

            profile = deepcopy(PROFILE)
            profile["answers"] = []
            profile["facts"] = [{
                "id": "answer-story-fact-current",
                "text": CURRENT_STORY_TEXT,
                "kind": "accomplishment",
                "source": "Synthetic fixture work history",
                "verified": True,
                "user_approved": True,
                "confidence": 100,
            }]
            store.save_profile(profile)

            other_profile = deepcopy(profile)
            other_profile.update({"id": "other", "name": "Other Synthetic Profile"})
            other_profile["facts"] = [{
                **profile["facts"][0],
                "id": "answer-story-fact-foreign",
                "text": "Managed synthetic storage for another profile.",
            }]
            store.save_profile(other_profile)

            story_content = {
                "kind": "STAR_STORY",
                "title": "Synthetic Linux service support",
                "text": CURRENT_STORY_TEXT,
                "fact_ids": ["answer-story-fact-current"],
                "tags": ["linux", "support"],
                "allowed_uses": ["APPLICATION_ANSWER"],
            }
            current_story = create_story(store, "fixture", story_content)
            approved_current = review_story(
                store, "fixture", current_story["story_id"], True,
                "Synthetic acceptance review", expected_revision=1,
            )
            self.assertEqual(approved_current["review_status"], "APPROVED")
            self.assertTrue(approved_current["reusable"])

            old_story = create_story(store, "fixture", {
                **story_content,
                "title": "Stale synthetic Linux service support",
            })
            review_story(store, "fixture", old_story["story_id"], True,
                         "Synthetic acceptance review", expected_revision=1)
            stale_revision = revise_story(
                store, "fixture", old_story["story_id"], {
                    **story_content,
                    "title": "New pending synthetic Linux service support",
                }, expected_revision=1,
            )
            self.assertEqual(stale_revision["revision"], 2)
            self.assertFalse(stale_revision["reusable"])

            foreign_story = create_story(store, "other", {
                **story_content,
                "title": "Foreign synthetic storage support",
                "text": "Managed synthetic storage for another profile.",
                "fact_ids": ["answer-story-fact-foreign"],
            })
            review_story(store, "other", foreign_story["story_id"], True,
                         "Synthetic acceptance review", expected_revision=1)

            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
                discover(store, fixture, "other")
            job_id = store.jobs()[0]["id"]
            application_id = prepare(store, job_id, "fixture", root)
            other_application_id = prepare(store, job_id, "other", root)

            app = create_app(
                store,
                "http://127.0.0.1:1",
                root,
                "redis://127.0.0.1:1/0",
                operation_mode="FIXTURE",
            )
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated story-reference API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    outside_requests = []
                    browser_writes = []
                    answer_match_requests = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            outside_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)

                    def observe_write(request):
                        method = request.method.upper()
                        if method == "POST" and request.url.endswith("/answers/match"):
                            answer_match_requests.append(request.url)
                        if method in {"PUT", "PATCH", "DELETE"} or (
                            method == "POST" and not request.url.endswith("/answers/match")
                        ):
                            browser_writes.append((method, request.url))

                    page.on("request", observe_write)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    self.open_application(page)

                    question_text = "How did you support synthetic Linux services?"
                    answer_box = self.add_question(page, question_text)
                    expect(answer_box).to_have_value("")

                    matching = page.get_by_role("region", name="Application answer matching", exact=True)
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith(ANSWER_MATCH_ROUTE.format(application_id=application_id))
                    ) as matched:
                        matching.get_by_role("button", name="Find saved answer matches", exact=True).click()
                    self.assertEqual(matched.value.status, 200, matched.value.text)
                    response_body = matched.value.json()
                    self.assertEqual(response_body["profile_id"], "fixture")
                    self.assertEqual(response_body["application_id"], application_id)
                    self.assertEqual(response_body["question"], question_text)
                    references = response_body["story_references"]
                    self.assertEqual([row["story_id"] for row in references], [current_story["story_id"]])
                    self.assertEqual(references[0]["revision"], 1)
                    self.assertEqual(references[0]["allowed_use"], "APPLICATION_ANSWER")
                    self.assertEqual(references[0]["review_status"], "APPROVED")
                    self.assertTrue(references[0]["is_current"])
                    self.assertEqual(references[0]["current_revision"], 1)
                    self.assertNotIn("answer_text", references[0])
                    self.assertEqual(references[0]["fact_provenance"], [{
                        "fact_id": "answer-story-fact-current",
                        "fingerprint": approved_current["fact_fingerprints"][0]["fingerprint"],
                    }])
                    self.assertNotIn(old_story["story_id"], {row["story_id"] for row in references})
                    self.assertNotIn(foreign_story["story_id"], {row["story_id"] for row in references})

                    reference_region = page.get_by_role("region", name="Saved story references", exact=True)
                    expect(reference_region).to_contain_text(current_story["story_id"])
                    expect(reference_region).to_contain_text("revision 1")
                    expect(reference_region).to_contain_text("answer-story-fact-current")
                    expect(reference_region).to_contain_text(approved_current["fact_fingerprints"][0]["fingerprint"])
                    expect(reference_region).not_to_contain_text(old_story["story_id"])
                    expect(reference_region).not_to_contain_text(foreign_story["story_id"])

                    reference_region.get_by_role("button", name="Use as story citation", exact=True).click()
                    selected_citation = page.get_by_role(
                        "region", name="Selected story citation", exact=True
                    )
                    expect(selected_citation).to_contain_text(current_story["story_id"])
                    expect(selected_citation).to_contain_text("revision 1")
                    expect(selected_citation).to_contain_text("answer-story-fact-current")
                    expect(selected_citation).to_contain_text(
                        approved_current["fact_fingerprints"][0]["fingerprint"]
                    )
                    expect(answer_box).to_have_value("")
                    review_gate = page.get_by_label("I reviewed this exact answer for this job", exact=True)
                    expect(review_gate).not_to_be_checked()
                    self.assertEqual(browser_writes, [], "Citation selection must not write or export answers")
                    self.assertEqual(outside_requests, [], "FIXTURE browser attempted a non-local request")

                    # Citation selection is temporary; reload reads the same scoped source list.
                    page.reload(wait_until="domcontentloaded")
                    self.open_application(page)
                    answer_box = self.add_question(page, question_text)
                    matching = page.get_by_role("region", name="Application answer matching", exact=True)
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith(ANSWER_MATCH_ROUTE.format(application_id=application_id))
                    ) as reloaded_match:
                        matching.get_by_role("button", name="Find saved answer matches", exact=True).click()
                    self.assertEqual(reloaded_match.value.status, 200, reloaded_match.value.text)
                    self.assertEqual(
                        [row["story_id"] for row in reloaded_match.value.json()["story_references"]],
                        [current_story["story_id"]],
                    )
                    expect(answer_box).to_have_value("")

                    # Sensitive questions never receive story references or trigger matching.
                    page.get_by_role("button", name="Add question", exact=True).click()
                    sensitive_question = page.get_by_label("Employer question", exact=True).nth(1)
                    matches_before_sensitive = len(answer_match_requests)
                    sensitive_question.fill("What is your ethnic origin?")
                    sensitive_draft = page.locator("fieldset.fact").last
                    expect(sensitive_draft.get_by_text("Sensitive or legal question", exact=False)).to_be_visible()
                    self.assertEqual(
                        sensitive_draft.get_by_role(
                            "region", name="Saved story references", exact=True
                        ).count(),
                        0,
                    )
                    self.assertEqual(browser_writes, [])
                    self.assertEqual(len(answer_match_requests), matches_before_sensitive)

                    # The service rejects an otherwise-valid application ID under a foreign profile.
                    mismatched_scope = context.request.post(
                        origin + f"/profiles/fixture/applications/{other_application_id}/answers/match",
                        data={"question": question_text},
                    )
                    self.assertEqual(mismatched_scope.status, 404, mismatched_scope.text())
                    other_scope = context.request.post(
                        origin + f"/profiles/other/applications/{other_application_id}/answers/match",
                        data={"question": question_text},
                    )
                    self.assertEqual(other_scope.status, 200, other_scope.text())
                    self.assertEqual(
                        [row["story_id"] for row in other_scope.json()["story_references"]],
                        [foreign_story["story_id"]],
                    )
                    self.assertNotIn(
                        current_story["story_id"],
                        {row["story_id"] for row in other_scope.json()["story_references"]},
                    )
                    self.assertEqual(outside_requests, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated answer-story API did not stop")

    @staticmethod
    def open_application(page):
        page.get_by_role("button", name="Applications", exact=False).click()
        page.locator(".job-card").first.click()

    @staticmethod
    def add_question(page, question):
        page.get_by_role("button", name="Add question", exact=True).click()
        question_box = page.get_by_label("Employer question", exact=True).last
        question_box.fill(question)
        return page.get_by_label("Answer", exact=True).last
