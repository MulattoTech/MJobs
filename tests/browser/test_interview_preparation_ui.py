"""Connected FIXTURE browser acceptance for the grounded interview-prep workspace."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
import unittest

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.career_profile.interview_preparation import list_interview_prep_packs
from services.career_profile.story_bank import create_story, review_story
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class InterviewPreparationUiBrowserTests(unittest.TestCase):
    def test_grounded_pack_retry_readback_and_deterministic_download(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-interview-prep-ui-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'interview-prep.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")

            profile = deepcopy(PROFILE)
            profile["facts"] = [{
                "id": "interview-fact-service-recovery",
                "text": "Reduced repeated Linux service incidents by documenting a recovery handoff.",
                "kind": "accomplishment",
                "source": "Synthetic fixture work history",
                "verified": True,
                "user_approved": True,
                "confidence": 100,
            }]
            store.save_profile(profile)
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]

            story = create_story(store, "fixture", {
                "kind": "STAR_STORY",
                "title": "Documented a Linux service recovery handoff",
                "text": "Reduced repeated Linux service incidents by documenting a recovery handoff.",
                "fact_ids": ["interview-fact-service-recovery"],
                "tags": ["service recovery"],
                "allowed_uses": ["INTERVIEW_PREPARATION"],
            })
            approved_story = review_story(
                store, "fixture", story["story_id"], True,
                "Synthetic acceptance fixture review", expected_revision=1,
            )
            self.assertTrue(approved_story["reusable"])

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
                        self.fail("Interview-preparation isolated API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)
                    outside_requests = []
                    submission_requests = []
                    write_requests = []
                    exact_readbacks = []
                    fail_after_first_commit = True

                    def local_only_and_simulate_ambiguous_write(route):
                        nonlocal fail_after_first_commit
                        request = route.request
                        if urlsplit(request.url).netloc != urlsplit(origin).netloc:
                            outside_requests.append(request.url)
                            route.abort()
                            return
                        if re.search(r"(?:/submit|/applications/.*/submit)(?:$|[/?])", urlsplit(request.url).path):
                            submission_requests.append(request.url)
                        if (request.method == "GET" and
                                "/interview-prep-packs/" in urlsplit(request.url).path):
                            exact_readbacks.append(request.url)
                        if (request.method == "POST" and
                                urlsplit(request.url).path.endswith("/interview-prep-packs")):
                            write_requests.append(request.post_data_json)
                            if fail_after_first_commit:
                                # Let the API commit, then simulate a lost response. The retry must
                                # resolve to that immutable tuple instead of creating another row.
                                response = route.fetch()
                                self.assertIn(response.status, (200, 201))
                                fail_after_first_commit = False
                                route.fulfill(
                                    status=503,
                                    content_type="application/json",
                                    body=json.dumps({"detail": "Synthetic response interruption after commit"}),
                                )
                                return
                        route.continue_()

                    context.route("**/*", local_only_and_simulate_ambiguous_write)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: self.fail(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")

                    self.open_interview_preparation(page, job["title"], job["id"])
                    workspace = page.get_by_role("region", name="Interview preparation", exact=True)
                    story_label = re.compile(
                        r"Documented a Linux service recovery handoff · revision 1.*"
                        r"career_story_revision:.*career_fact:interview-fact-service-recovery",
                        re.DOTALL,
                    )
                    story_checkbox = workspace.get_by_role("checkbox", name=story_label)
                    expect(story_checkbox).not_to_be_checked()
                    expect(workspace).to_contain_text("Prompts help you prepare; they do not write answers for you.")
                    expect(workspace.get_by_role(
                        "button", name="Download deterministic export", exact=True,
                    )).to_have_count(0)
                    story_checkbox.check()
                    expect(story_checkbox).to_be_checked()

                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith("/interview-prep-packs")
                    ) as first_write:
                        workspace.get_by_role("button", name="Create interview prep pack", exact=True).click()
                    self.assertEqual(first_write.value.status, 503)
                    expect(workspace.get_by_role("alert")).to_contain_text("Could not load or save")
                    expect(story_checkbox).to_be_checked()

                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith("/interview-prep-packs")
                    ) as retry_write:
                        workspace.get_by_role("button", name="Create interview prep pack", exact=True).click()
                    self.assertIn(retry_write.value.status, (200, 201))

                    pack_article = workspace.get_by_role("article", name="Interview prep pack version 1", exact=True)
                    expect(pack_article).to_be_visible()
                    expect(pack_article).to_contain_text("career_fact:interview-fact-service-recovery")
                    expect(pack_article.get_by_role("region", name="Missing evidence", exact=True)).to_be_visible()
                    expect(pack_article).to_contain_text("No selected current approved interview story links evidence")
                    preview = pack_article.get_by_role("region", name="Deterministic export preview", exact=True)
                    expect(preview).to_contain_text("Grounded talking points")
                    preview_text = preview.locator("pre").inner_text()
                    preview_hash = hashlib.sha256(preview_text.encode("utf-8")).hexdigest()
                    self.assertEqual(write_requests, [{
                        "selected_story_revision_ids": [approved_story["story_id"]],
                    }] * 2)
                    persisted = list_interview_prep_packs(store, "fixture", job["id"])
                    self.assertEqual(len(persisted), 1)
                    self.assertTrue(exact_readbacks, "Create should confirm the exact persisted pack route")

                    readbacks_before_reload = len(exact_readbacks)
                    page.reload(wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    self.open_interview_preparation(page, job["title"], job["id"])
                    workspace = page.get_by_role("region", name="Interview preparation", exact=True)
                    pack_article = workspace.get_by_role("article", name="Interview prep pack version 1", exact=True)
                    preview = pack_article.get_by_role("region", name="Deterministic export preview", exact=True)
                    reloaded_text = preview.locator("pre").inner_text()
                    self.assertEqual(hashlib.sha256(reloaded_text.encode("utf-8")).hexdigest(), preview_hash)
                    self.assertEqual(
                        workspace.get_by_role("button", name=re.compile(r"^Version 1 ·")).count(),
                        1,
                    )
                    self.assertGreater(len(exact_readbacks), readbacks_before_reload)
                    self.assertTrue(
                        exact_readbacks[-1].endswith("/" + persisted[0]["pack_id"]),
                        "Reload should read back this exact saved pack version",
                    )

                    with page.expect_download() as download_info:
                        preview.get_by_role("button", name="Download deterministic export", exact=True).click()
                    download = download_info.value
                    self.assertEqual(download.suggested_filename, "interview-preparation-v1.txt")
                    downloaded_path = root / download.suggested_filename
                    download.save_as(downloaded_path)
                    self.assertEqual(downloaded_path.read_text(encoding="utf-8"), reloaded_text)
                    self.assertEqual(len(list_interview_prep_packs(store, "fixture", job["id"])), 1)
                    self.assertFalse(outside_requests, "FIXTURE browser attempted an external request")
                    self.assertFalse(submission_requests, "Interview preparation must not submit an application")

                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    @staticmethod
    def open_interview_preparation(page, job_title, job_id):
        page.get_by_role("navigation", name="Main navigation").get_by_role(
            "button", name=re.compile(r"^Jobs"),
        ).click()
        page.locator(".job-card").filter(has_text=job_title).first.click()
        workspace = page.get_by_role("region", name="Interview preparation", exact=True)
        expect(workspace).to_be_visible()
        target = workspace.get_by_role("region", name="Selected interview target", exact=True)
        expect(target).to_contain_text(job_title)
        expect(target).to_contain_text(f"Job ID: {job_id}")


if __name__ == "__main__":
    unittest.main()
