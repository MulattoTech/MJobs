"""Connected browser acceptance for explicit, durable resume story reuse."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.career_profile.story_bank import create_story, review_story
from services.discovery.pipeline import discover
from services.documents.tailor import target_fingerprint
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ResumeStoryReuseBrowserTests(unittest.TestCase):
    def test_explicit_story_selection_generates_reviewable_resume_and_survives_readback(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-resume-story-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'stories.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")
            job = store.jobs()[0]
            profile = store.profile("fixture")
            fact = next(row for row in profile["facts"]
                        if row.get("fact_id", row.get("id")) == "python")
            story_text = (
                "Situation: a synthetic Python support queue was slow. Task: improve it.\n"
                "Action: I optimized the reviewed workflow. Result: responses became faster."
            )
            story = create_story(store, "fixture", {
                "kind": "ACCOMPLISHMENT",
                "title": "Synthetic Python workflow improvement",
                "text": story_text,
                "fact_ids": [fact.get("fact_id", fact.get("id"))],
                "tags": ["support"],
                "allowed_uses": ["RESUME"],
            })
            story = review_story(
                store, "fixture", story["story_id"], True, None,
                expected_revision=story["revision"],
            )

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(
                store, "http://127.0.0.1:1", root,
                "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
            )
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Resume story isolated API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    external_requests = []
                    submission_requests = []

                    def local_only(route):
                        request_url = route.request.url
                        if urlsplit(request_url).netloc != urlsplit(origin).netloc:
                            external_requests.append(request_url)
                            route.abort()
                        else:
                            if "/submit" in urlsplit(request_url).path:
                                submission_requests.append(request_url)
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    navigation = page.get_by_role("navigation", name="Main navigation")
                    navigation.get_by_role("button", name="Jobs", exact=False).click()
                    page.locator(".job-card").filter(has_text=job["title"]).click()

                    selection = page.get_by_role(
                        "region", name=f"Resume career story selection for {job['title']}", exact=True,
                    )
                    checkbox = selection.get_by_role(
                        "checkbox",
                        name=f"Use {story['title']} revision {story['revision']} for resume",
                        exact=True,
                    )
                    selection.get_by_text("Review story text", exact=True).click()
                    expect(selection).to_contain_text(story_text)
                    expect(checkbox).not_to_be_checked()
                    checkbox.check()
                    expect(selection).to_contain_text("1 story revision selected")
                    expect(selection).to_contain_text("Approved fact reference: python")
                    expect(page.get_by_text(
                        "Selected story wording appears exactly as you wrote it in the resume"
                    )).to_be_visible()

                    with page.expect_response(
                        lambda response: response.url == origin + "/applications"
                        and response.request.method == "POST"
                    ) as queued:
                        page.get_by_role("button", name="Prepare documents", exact=True).click()
                    operation = queued.value.json()
                    body = queued.value.request.post_data_json
                    self.assertEqual(body["resume_story_ids"], [story["story_id"]])
                    self.assertNotIn("cover_letter_story_ids", body)
                    self.assertEqual(body["expected_profile_version"], profile["version"])
                    self.assertEqual(body["expected_job_fingerprint"], target_fingerprint(job))

                    with patch.dict(
                        "os.environ", MJOBS_DATABASE_URL=store.url,
                        MJOBS_MOCK_ORIGIN="http://127.0.0.1:1",
                        MJOBS_OUTPUT_DIR=str(root), MJOBS_OPERATION_MODE="FIXTURE",
                    ):
                        from workers.tasks import execute_operation
                        outcome = execute_operation(operation["operation_id"])
                    self.assertEqual(outcome["status"], "COMPLETED", outcome)

                    response = context.request.get(
                        origin + f"/profiles/fixture/applications/{operation['application_id']}/documents"
                    )
                    self.assertEqual(response.status, 200, response.text())
                    resume = next(row for row in response.json()["documents"]
                                  if row["kind"] == "resume")
                    self.assertIsNone(resume["preview_error"])
                    self.assertIn(story_text, resume["preview_text"])
                    refs = resume["generation_context"]["story_refs"]
                    self.assertEqual([row["story_id"] for row in refs], [story["story_id"]])
                    self.assertNotIn(story_text, str(resume["generation_context"]))
                    page.reload(wait_until="domcontentloaded")
                    navigation.get_by_role("button", name="Applications", exact=False).click()
                    page.locator(".job-card").filter(has_text=job["title"]).click()
                    resume_region = page.get_by_role(
                        "region", name="resume version 1", exact=True,
                    )
                    expect(resume_region).to_contain_text("Synthetic Python workflow improvement")
                    expect(resume_region).to_contain_text("exact user-authored wording")
                    expect(resume_region).to_contain_text("manual review required")
                    expect(resume_region).to_contain_text(story_text)
                    self.assertEqual(external_requests, [])
                    self.assertEqual(submission_requests, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
