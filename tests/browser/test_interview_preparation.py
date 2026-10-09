"""Connected browser acceptance for persisted, truthful interview prep packs."""
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.career_profile.story_bank import create_story, review_story
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class InterviewPreparationBrowserTests(unittest.TestCase):
    def test_create_export_and_reload_grounded_pack_without_external_submission(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-interview-prep-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'interview-prep.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = deepcopy(PROFILE)
            profile["facts"] = [{
                "id": "browser-prep-fact",
                "text": "Maintained synthetic Python support services for four test teams.",
                "kind": "accomplishment",
                "source": "Synthetic fixture work history",
                "verified": True,
                "user_approved": True,
                "confidence": 100,
            }]
            store.save_profile(profile)
            other_profile = deepcopy(profile)
            other_profile.update({"id": "other", "name": "Other Synthetic Profile"})
            store.save_profile(other_profile)
            job_id = "interview-prep-browser-job"
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "interview-prep-browser-company", "name": "Synthetic Systems",
                    "domain": "example.test",
                })
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": "interview-prep-browser-company",
                    "requisition": job_id, "title": "Synthetic Python Support Engineer",
                    "description": "Synthetic role requiring Python incident response and support.",
                    "skills": ["Python", "incident response"], "location": "Remote",
                    "remote_type": "REMOTE", "attributes": {},
                })
                upsert(connection, m.routes, {
                    "id": "interview-prep-browser-route", "job_id": job_id,
                    "company_id": "interview-prep-browser-company", "provider": "fixture",
                    "tenant": "interview-prep-browser", "requisition": job_id,
                    "url": "https://jobs.example.test/interview-prep-browser",
                })
            story = create_story(store, "fixture", {
                "kind": "STAR_STORY",
                "title": "Synthetic service recovery story",
                "text": "User-authored synthetic story: restored a test service after a configuration regression.",
                "fact_ids": ["browser-prep-fact"],
                "tags": ["python", "support"],
                "allowed_uses": ["INTERVIEW_PREPARATION"],
            })
            story = review_story(
                store, "fixture", story["story_id"], True, None,
                expected_revision=story["revision"],
            )

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(
                store,
                "http://127.0.0.1:1",
                root,
                "redis://127.0.0.1:1/0",
                operation_mode="FIXTURE",
            )
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Interview-preparation isolated API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)
                    external_requests = []
                    submission_requests = []

                    def local_only(route):
                        request = route.request
                        if urlsplit(request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                            return
                        if re.search(r"submit|submission", request.url, re.IGNORECASE) or (
                            request.method != "GET"
                            and re.search(r"application|ats", request.url, re.IGNORECASE)
                        ):
                            submission_requests.append((request.method, request.url))
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")

                    jobs_tab = page.get_by_role("navigation").get_by_role("button", name="Jobs", exact=True)
                    if jobs_tab.count():
                        jobs_tab.click()
                    page.locator(".job-card").filter(has_text="Synthetic Python Support Engineer").first.click()

                    prep = page.get_by_role("region", name="Interview preparation", exact=True)
                    expect(prep).to_be_visible()
                    expect(prep).to_contain_text("No interview prep packs have been saved for this job.")
                    story_choice = prep.get_by_label(
                        re.compile(r"Synthetic service recovery story · revision 1"), exact=False,
                    )
                    story_choice.check()
                    with page.expect_response(
                        lambda response: response.url.endswith("/interview-prep-packs")
                        and response.request.method == "POST"
                    ) as create_response, page.expect_response(
                        lambda response: "/interview-prep-packs/" in response.url
                        and not response.url.endswith("/interview-prep-packs")
                        and response.request.method == "GET"
                    ) as detail_response:
                        prep.get_by_role("button", name="Create interview prep pack", exact=True).click()
                    self.assertEqual(create_response.value.status, 200, create_response.value.text)
                    pack = create_response.value.json()
                    self.assertEqual(pack["profile_id"], "fixture")
                    self.assertEqual(pack["job_id"], job_id)
                    self.assertEqual(pack["selected_story_revision_ids"], [story["story_id"]])
                    self.assertTrue(pack["export_text"].strip())

                    self.assertTrue(detail_response.value.url.endswith(f"/interview-prep-packs/{pack['pack_id']}"))
                    self.assertEqual(detail_response.value.status, 200)
                    article = page.locator('article[aria-label="Interview prep pack version 1"]').first
                    self.assertEqual(article.count(), 1)
                    expect(article).to_be_visible()
                    expect(article.get_by_role("region", name="Missing evidence", exact=True)).to_be_visible()
                    expect(article).to_contain_text("Synthetic service recovery story")
                    expect(article).to_contain_text("It contains no generated answers.")

                    preview = article.get_by_role("region", name="Deterministic export preview", exact=True)
                    with page.expect_download() as download_info:
                        preview.get_by_role("button", name="Download deterministic export", exact=True).click()
                    download = download_info.value
                    self.assertEqual(download.suggested_filename, "interview-preparation-v1.txt")
                    self.assertEqual(Path(download.path()).read_text(encoding="utf-8"), pack["export_text"])

                    page.reload(wait_until="domcontentloaded")
                    if jobs_tab.count():
                        jobs_tab = page.get_by_role("navigation").get_by_role("button", name="Jobs", exact=True)
                        jobs_tab.click()
                    page.locator(".job-card").filter(has_text="Synthetic Python Support Engineer").first.click()
                    prep = page.get_by_role("region", name="Interview preparation", exact=True)
                    saved_article = page.locator('article[aria-label="Interview prep pack version 1"]').first
                    expect(saved_article).to_be_visible()
                    expect(saved_article).to_contain_text("Synthetic service recovery story")
                    expect(prep).to_contain_text("Synthetic service recovery story")
                    self.assertFalse(external_requests, "FIXTURE browser attempted an external request")
                    self.assertFalse(submission_requests, "Interview preparation attempted an application submission")
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
