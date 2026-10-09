"""Connected browser acceptance for scoped, review-only match feedback."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from services.discovery.search_profiles import save_search_profile
from services.discovery.search_semantics import evaluate_search
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
FEEDBACK_CATEGORIES = {
    "GOOD_MATCH": "Good match",
    "BAD_MATCH": "Bad match",
    "TOO_SENIOR": "Too senior",
    "WRONG_TECHNOLOGY": "Wrong technology",
    "WRONG_LOCATION": "Wrong location",
    "BAD_SALARY": "Bad salary",
    "BAD_COMPANY": "Bad company",
    "NOT_INTERESTED": "Not interested",
}


def _feedback_bundle_is_mounted() -> bool:
    """Do not mistake current main's stale/unmounted web assets for acceptance."""
    web_source = (ROOT / "apps/web/src/main.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    index = ROOT / "apps/web/dist/index.html"
    assets = ROOT / "apps/web/dist/assets"
    if ("MatchFeedbackWorkflow" not in web_source
            or "create_match_feedback_router" not in api_source
            or not index.is_file() or not assets.is_dir()):
        return False
    bundles = list(assets.glob("*.js"))
    return any("Job match feedback" in bundle.read_text(
        encoding="utf-8", errors="ignore") for bundle in bundles)


def _save_search(store, *, profile_id: str, search_id: str) -> None:
    save_search_profile(store, {
        "id": search_id,
        "profile_id": profile_id,
        "name": search_id,
        "enabled": False,
        "include_titles": ["Synthetic"],
        "exclude_titles": [],
        "required_keywords": ["Linux"],
        "optional_keywords": [],
        "excluded_keywords": [],
        "exact_phrases": [],
        "technology_aliases": {},
        "remote": "ANY",
        "employment_types": [],
        "source_ids": [],
    })


def _preference_and_eligibility_state(store) -> dict:
    """Capture the hard-filter and saved preference state, excluding feedback history."""
    profile_ids = ("fixture", "other")
    preferences = {
        profile_id: {
            "version": store.profile(profile_id)["version"],
            "preferences": deepcopy(store.profile(profile_id).get("preferences", {})),
        }
        for profile_id in profile_ids
    }
    with store.engine.connect() as connection:
        searches = connection.execute(select(m.search_profiles).where(
            m.search_profiles.c.profile_id.in_(profile_ids),
        ).order_by(m.search_profiles.c.id)).mappings().all()
        jobs = connection.execute(select(
            m.jobs, m.companies.c.name.label("company"),
        ).select_from(m.jobs.join(
            m.companies, m.companies.c.id == m.jobs.c.company_id,
        )).order_by(m.jobs.c.id)).mappings().all()
        matches = connection.execute(select(m.matches).order_by(
            m.matches.c.profile_id, m.matches.c.job_id,
        )).mappings().all()

    hard_eligibility = {}
    search_criteria = {}
    for search in searches:
        criteria = search["criteria"]
        search_criteria[search["id"]] = deepcopy(criteria)
        hard_eligibility[search["id"]] = [
            job["id"] for job in jobs if evaluate_search(criteria, dict(job))["matched"]
        ]
    return {
        "preferences": preferences,
        "search_criteria": search_criteria,
        "hard_eligibility": hard_eligibility,
        "matches": [dict(row) for row in matches],
    }


@unittest.skipUnless(
    _feedback_bundle_is_mounted(),
    "NOT_RUN: root must mount MatchFeedbackWorkflow and the match-feedback API, then rebuild apps/web",
)
class MatchFeedbackWorkflowBrowserTests(unittest.TestCase):
    def test_categories_retry_readback_isolation_and_review_only_preferences(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-match-feedback-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'match-feedback.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")

            primary = deepcopy(PROFILE)
            primary["preferences"] = {
                **primary.get("preferences", {}),
                "desired_roles": ["Synthetic Systems Administrator"],
            }
            other = deepcopy(PROFILE)
            other.update(
                id="other", name="Other Synthetic Feedback Profile",
                email="other-feedback@example.test", facts=[],
            )
            other["preferences"] = {
                **other.get("preferences", {}),
                "desired_roles": ["Synthetic Systems Administrator"],
            }
            store.save_profile(primary)
            store.save_profile(other)
            _save_search(store, profile_id="fixture", search_id="fixture-feedback-search")
            _save_search(store, profile_id="other", search_id="other-feedback-search")

            first_job = capture_manual_job(store, {
                "request_id": "feedback-fixture-job-one",
                "title": "Synthetic Systems Administrator Feedback Role",
                "company": "Synthetic Feedback Employer",
                "description": "A synthetic Linux systems administration role.",
                "reference_url": "https://jobs.example.test/feedback-one",
                "remote_type": "REMOTE",
                "profile_id": "fixture",
            })["job"]
            second_job = capture_manual_job(store, {
                "request_id": "feedback-fixture-job-two",
                "title": "Synthetic Linux Project Coordinator Feedback Role",
                "company": "Synthetic Feedback Employer",
                "description": "A synthetic project coordination role supporting Linux teams.",
                "reference_url": "https://jobs.example.test/feedback-two",
                "remote_type": "REMOTE",
                "profile_id": "fixture",
            })["job"]
            recalculate(store, "fixture")
            recalculate(store, "other")
            first_job_id = first_job["id"]
            second_job_id = second_job["id"]
            baseline = _preference_and_eligibility_state(store)
            self.assertIn(first_job_id, baseline["hard_eligibility"]["fixture-feedback-search"])
            self.assertIn(second_job_id, baseline["hard_eligibility"]["fixture-feedback-search"])

            app = create_app(
                store, "http://127.0.0.1:1", root, "redis://127.0.0.1:1/0",
                operation_mode="FIXTURE",
            )
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            origin_netloc = urlsplit(origin).netloc
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            requests = []
            external_requests = []
            blocked_writes = []
            submission_requests = []
            actual_first_attempts = []
            fail_first_saved_response = True
            feedback_template = "/profiles/{profile_id}/jobs/{job_id}/match-feedback"

            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Synthetic local API did not start")
                    time.sleep(0.02)
                state_before_browser = _preference_and_eligibility_state(store)
                self.assertEqual(state_before_browser, baseline)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script("localStorage.setItem('mjobs-profile','fixture')")

                    def local_only(route):
                        nonlocal fail_first_saved_response
                        request = route.request
                        parsed = urlsplit(request.url)
                        path = parsed.path
                        if parsed.netloc != origin_netloc:
                            external_requests.append(request.url)
                            route.abort()
                            return
                        requests.append({
                            "method": request.method,
                            "path": path,
                            "body": request.post_data_json if request.post_data else None,
                        })
                        if any(token in path.lower() for token in (
                            "/submit", "/applications/submit", "/mock-run", "/apply",
                        )):
                            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                                submission_requests.append((request.method, path))
                                route.abort()
                                return
                        if path.endswith("/match-feedback") and request.method == "POST":
                            if (fail_first_saved_response
                                    and request.post_data_json.get("category") == "BAD_MATCH"):
                                fail_first_saved_response = False
                                saved = route.fetch()
                                actual_first_attempts.append({
                                    "status": saved.status,
                                    "body": saved.json(),
                                })
                                route.fulfill(
                                    status=503,
                                    content_type="application/json",
                                    body=json.dumps({"detail": "Synthetic response loss after save"}),
                                )
                                return
                            route.continue_()
                            return
                        if request.method not in {"GET", "HEAD", "OPTIONS"}:
                            blocked_writes.append((request.method, path))
                            route.abort()
                            return
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    page.get_by_role("button", name="Jobs", exact=True).click()
                    page.get_by_label("Saved search profile", exact=True).select_option(
                        "fixture-feedback-search",
                    )
                    first_card = page.locator(".job-card").filter(
                        has_text="Synthetic Systems Administrator Feedback Role",
                    )
                    first_card.click()
                    feedback = page.get_by_role("region", name="Job match feedback")
                    category = feedback.get_by_label("Feedback category", exact=True)
                    save = feedback.get_by_role("button", name="Save feedback", exact=True)
                    feedback_url = feedback_template.format(
                        profile_id="fixture", job_id=first_job_id,
                    )

                    category.select_option("BAD_MATCH")
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == feedback_url
                            and response.request.method == "POST") as initial_attempt:
                        save.click()
                    self.assertEqual(initial_attempt.value.status, 503)
                    self.assertEqual(_preference_and_eligibility_state(store), baseline)
                    retry = feedback.get_by_role("button", name="Retry feedback", exact=True)
                    expect(retry).to_be_visible()
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == feedback_url
                            and response.request.method == "POST") as retry_attempt:
                        retry.click()
                    self.assertLess(retry_attempt.value.status, 300, retry_attempt.value.text())
                    replay = retry_attempt.value.json()
                    self.assertTrue(replay["replayed"])
                    self.assertEqual(replay["feedback"]["category"], "BAD_MATCH")
                    self.assertEqual(len(actual_first_attempts), 1)
                    self.assertLess(actual_first_attempts[0]["status"], 300)
                    self.assertFalse(actual_first_attempts[0]["body"]["replayed"])
                    feedback_posts = [item for item in requests
                                      if item["path"] == feedback_url and item["method"] == "POST"]
                    self.assertEqual(len(feedback_posts), 2)
                    first_request_id = feedback_posts[0]["body"]["request_id"]
                    self.assertTrue(first_request_id)
                    self.assertEqual(feedback_posts[1]["body"]["request_id"], first_request_id)
                    self.assertEqual(feedback_posts[0]["body"]["category"], "BAD_MATCH")

                    proposal = replay["preference_proposal"]
                    self.assertIsNotNone(proposal, "BAD_MATCH should offer a reviewable preference proposal")
                    self.assertTrue(proposal["review_required"])
                    proposal_panel = feedback.get_by_role("region", name="Preference proposal")
                    expect(proposal_panel).to_be_visible()
                    expect(proposal_panel).to_contain_text(proposal["explanation"])
                    expect(proposal_panel).to_contain_text(proposal["dimension"])
                    expect(proposal_panel).to_contain_text(proposal["direction"])
                    expect(proposal_panel).to_contain_text("Review required")
                    expect(feedback.get_by_role(
                        "button", name="Review preference proposal", exact=True,
                    )).to_be_enabled()
                    self.assertEqual(_preference_and_eligibility_state(store), baseline)
                    feedback.get_by_role(
                        "button", name="Review preference proposal", exact=True,
                    ).click()
                    self.assertEqual(_preference_and_eligibility_state(store), baseline)

                    for feedback_category, label in FEEDBACK_CATEGORIES.items():
                        if feedback_category == "BAD_MATCH":
                            continue
                        category.select_option(feedback_category)
                        with page.expect_response(lambda response:
                                urlsplit(response.url).path == feedback_url
                                and response.request.method == "POST") as saved_feedback:
                            save.click()
                        self.assertLess(saved_feedback.value.status, 300, saved_feedback.value.text())
                        body = saved_feedback.value.json()
                        self.assertEqual(body["profile_id"], "fixture")
                        self.assertEqual(body["job_id"], first_job_id)
                        self.assertEqual(body["feedback"]["category"], feedback_category)
                        self.assertTrue(body["feedback"]["id"])
                        if body["preference_proposal"] is not None:
                            self.assertTrue(body["preference_proposal"]["review_required"])
                            review_region = feedback.get_by_role(
                                "region", name="Preference proposal",
                            )
                            expect(review_region).to_be_visible()
                            feedback.get_by_role(
                                "button", name="Review preference proposal", exact=True,
                            ).click()
                        self.assertEqual(_preference_and_eligibility_state(store), baseline)
                        expect(feedback).to_contain_text(label)

                    # Reload must repopulate saved feedback by GET for this exact
                    # profile/job pair; matching history reads remain local/read-only.
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Jobs", exact=True).click()
                    page.get_by_label("Saved search profile", exact=True).select_option(
                        "fixture-feedback-search",
                    )
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == feedback_url
                            and response.request.method == "GET") as readback:
                        page.locator(".job-card").filter(
                            has_text="Synthetic Systems Administrator Feedback Role",
                        ).click()
                    self.assertEqual(readback.value.status, 200, readback.value.text())
                    history = readback.value.json()
                    self.assertEqual(history["profile_id"], "fixture")
                    self.assertEqual(history["job_id"], first_job_id)
                    self.assertEqual({row["category"] for row in history["items"]},
                                     set(FEEDBACK_CATEGORIES))
                    feedback = page.get_by_role("region", name="Job match feedback")
                    for label in FEEDBACK_CATEGORIES.values():
                        expect(feedback).to_contain_text(label)

                    second_feedback_url = feedback_template.format(
                        profile_id="fixture", job_id=second_job_id,
                    )
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == second_feedback_url
                            and response.request.method == "GET") as second_job_history:
                        page.locator(".job-card").filter(
                            has_text="Synthetic Linux Project Coordinator Feedback Role",
                        ).click()
                    self.assertEqual(second_job_history.value.status, 200)
                    self.assertEqual(second_job_history.value.json()["items"], [])
                    self.assertIsNone(second_job_history.value.json()["latest"])
                    expect(page.get_by_role("region", name="Job match feedback")).not_to_contain_text(
                        "Bad match",
                    )

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    page.get_by_role("button", name="Jobs", exact=True).click()
                    page.get_by_label("Saved search profile", exact=True).select_option(
                        "other-feedback-search",
                    )
                    other_feedback_url = feedback_template.format(
                        profile_id="other", job_id=first_job_id,
                    )
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == other_feedback_url
                            and response.request.method == "GET") as other_profile_history:
                        page.locator(".job-card").filter(
                            has_text="Synthetic Systems Administrator Feedback Role",
                        ).click()
                    self.assertEqual(other_profile_history.value.status, 200)
                    self.assertEqual(other_profile_history.value.json()["items"], [])
                    expect(page.get_by_role("region", name="Job match feedback")).not_to_contain_text(
                        "Bad match",
                    )

                    self.assertFalse(fail_first_saved_response)
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(external_requests, [])
                    self.assertEqual(submission_requests, [])
                    self.assertTrue(any(
                        item["method"] == "GET" and item["path"] == feedback_url
                        for item in requests
                    ))
                    browser.close()

                self.assertEqual(_preference_and_eligibility_state(store), baseline)
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Synthetic local API did not stop")


if __name__ == "__main__":
    unittest.main()
