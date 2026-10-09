"""Connected browser acceptance for durable, manual-only application pages."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

import uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
PAGES = ["contact", "experience"]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class MultipageApplicationRecoveryBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-multipage-application-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'multipage.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Synthetic Profile",
                                 "email": "other@example.invalid"})
        self.fixture = fixture_server()
        self.fixture_origin = self.fixture.__enter__()
        discover(self.store, self.fixture_origin, "fixture")
        self.job = self.store.jobs("fixture")[0]
        self.application_id = prepare(self.store, self.job["id"], "fixture", self.root)
        self.application_url = (
            f"/profiles/fixture/applications/{self.application_id}/multipage-state"
        )
        self.app = create_app(self.store, "http://127.0.0.1:1", self.root,
                              operation_mode="FIXTURE")
        self.client = TestClient(self.app)
        initialized = self.client.post(self.application_url, json={
            "request_id": "browser-initialize-contact-1",
            "action": "SAVE_PROGRESS",
            "expected_version": 0,
            "page_id": "contact",
            "page_order": PAGES,
            "progress": {},
        })
        self.assertEqual(initialized.status_code, 200, initialized.text)
        self.api_socket = bound_browser_socket()
        self.origin = f"http://127.0.0.1:{self.api_socket.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error"))
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.api_socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.fail("Isolated application browser server did not start")
            time.sleep(0.02)

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.should_exit = True
            self.thread.join(timeout=10)
            self.api_socket.close()
            self.assertFalse(self.thread.is_alive())
        if hasattr(self, "client"):
            self.client.close()
        if hasattr(self, "store"):
            self.store.engine.dispose()
        if hasattr(self, "fixture"):
            self.fixture.__exit__(None, None, None)
        self.temp.cleanup()

    def _open_application(self, page):
        page.get_by_role("button", name="Applications", exact=False).click()
        page.get_by_role("region", name="Not tracked").get_by_role(
            "button", name=self.job["title"], exact=False).click()
        return page.get_by_role("region", name="Multi-page application review")

    def test_pages_save_reload_retry_manual_review_and_never_submit(self):
        outside_requests = []
        requests = []
        injected_save_failures = 0
        injected_next_failures = 0
        durable_state_before_failed_next = []

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")
            def local_only(route):
                nonlocal injected_save_failures, injected_next_failures
                request = route.request
                parsed = urlsplit(request.url)
                requests.append((request.method, parsed.path))
                if parsed.netloc != urlsplit(self.origin).netloc:
                    outside_requests.append(request.url)
                    route.abort()
                    return
                if (request.method == "POST" and parsed.path == self.application_url
                        and request.post_data_json.get("action") == "SAVE_PROGRESS"
                        and injected_save_failures == 0):
                    injected_save_failures += 1
                    route.fulfill(status=503, content_type="application/json",
                                  body='{"detail":"temporary local fixture interruption"}')
                    return
                if (request.method == "POST" and parsed.path == self.application_url
                        and request.post_data_json.get("action") == "NEXT"
                        and injected_next_failures == 0):
                    # The preceding SAVE_PROGRESS in the two-step navigation has
                    # committed. Fail only the transition so reload must recover
                    # from the durable source of truth before retrying Continue.
                    durable_state_before_failed_next.append(
                        self.client.get(self.application_url).json())
                    injected_next_failures += 1
                    route.fulfill(status=503, content_type="application/json",
                                  body='{"detail":"local next-page transition interrupted"}')
                    return
                route.continue_()

            context.route("**/*", local_only)
            page = context.new_page()
            page.set_default_timeout(8000)
            page.goto(self.origin, wait_until="domcontentloaded")

            review = self._open_application(page)
            expect(review.get_by_role("heading", name="Contact information")).to_be_visible()
            expect(review.get_by_role("list", name="Application progress")).to_contain_text(
                "Contact information")
            expect(review.get_by_role("list", name="Application progress")).to_contain_text(
                "Experience")

            # A valid application id cannot be used through a different profile scope.
            foreign_response = page.evaluate("""async ({url, payload}) => {
              const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)});
              return {status: response.status};
            }""", {
                "url": self.origin + self.application_url.replace("/profiles/fixture/", "/profiles/other/"),
                "payload": {
                    "request_id": "browser-foreign-profile-write-1",
                    "action": "SAVE_PROGRESS",
                    "expected_version": 1,
                    "page_id": "contact",
                    "progress": {"contact": {"email": "unauthorized@example.invalid"}},
                },
            })
            self.assertEqual(foreign_response["status"], 404)
            state_after_foreign_write = self.client.get(self.application_url).json()
            self.assertEqual(state_after_foreign_write["current_page_id"], "contact")
            self.assertEqual(state_after_foreign_write["progress"], {})

            # Required input blocks navigation without changing durable page state.
            expect(review.get_by_role("button", name="Continue", exact=True)).to_be_disabled()
            expect(review.get_by_text("Required to continue: Email.", exact=True)).to_be_visible()
            self.assertEqual(self.client.get(self.application_url).json()["current_page_id"],
                             "contact")

            review.get_by_label("Email", exact=True).fill("synthetic.applicant@example.invalid")
            review.get_by_label("Phone", exact=True).fill("555-0101")
            review.get_by_role("button", name="Save progress", exact=True).click()
            expect(review.get_by_role("alert")).to_be_visible()

            # A failed write leaves the draft available; retry persists it.
            review.get_by_role("button", name="Save progress", exact=True).click()
            expect(review.get_by_role("alert")).to_have_count(0)
            saved = self.client.get(self.application_url).json()
            self.assertEqual(saved["current_page_id"], "contact")
            self.assertEqual(saved["progress"]["contact"], {
                "email": "synthetic.applicant@example.invalid", "phone": "555-0101"})

            page.reload(wait_until="domcontentloaded")
            review = self._open_application(page)
            expect(review.get_by_label("Email", exact=True)).to_have_value(
                "synthetic.applicant@example.invalid")
            expect(review.get_by_label("Phone", exact=True)).to_have_value("555-0101")

            review.get_by_role("button", name="Continue", exact=True).click()
            expect(review.get_by_role("alert")).to_be_visible()
            self.assertEqual(len(durable_state_before_failed_next), 1)
            durable_contact = durable_state_before_failed_next[0]
            self.assertEqual(durable_contact["current_page_id"], "contact")
            self.assertEqual(durable_contact["progress"]["contact"], {
                "email": "synthetic.applicant@example.invalid", "phone": "555-0101"})

            # The save completed before navigation failed. Reload restores that
            # durable progress, and the user can retry Continue safely.
            page.reload(wait_until="domcontentloaded")
            review = self._open_application(page)
            expect(review.get_by_role("heading", name="Contact information")).to_be_visible()
            expect(review.get_by_label("Email", exact=True)).to_have_value(
                "synthetic.applicant@example.invalid")
            expect(review.get_by_label("Phone", exact=True)).to_have_value("555-0101")
            review.get_by_role("button", name="Continue", exact=True).click()
            expect(review.get_by_role("heading", name="Experience", exact=True)).to_be_visible()
            experience = self.client.get(self.application_url).json()
            self.assertEqual(experience["current_page_id"], "experience")
            self.assertEqual(experience["progress"]["contact"]["email"],
                             "synthetic.applicant@example.invalid")

            review.get_by_label("Experience summary", exact=True).fill(
                "Synthetic experience answer for browser acceptance.")
            review.get_by_role("button", name="Save progress", exact=True).click()
            review.get_by_role("button", name="Previous", exact=True).click()
            expect(review.get_by_role("heading", name="Contact information")).to_be_visible()
            expect(review.get_by_label("Email", exact=True)).to_have_value(
                "synthetic.applicant@example.invalid")
            review.get_by_role("button", name="Continue", exact=True).click()
            expect(review.get_by_role("heading", name="Experience", exact=True)).to_be_visible()
            expect(review.get_by_label("Experience summary", exact=True)).to_have_value(
                "Synthetic experience answer for browser acceptance.")

            # The final action enters manual review and never exposes submission.
            review.get_by_role("button", name="Continue", exact=True).click()
            expect(review.get_by_text("Manual intervention required", exact=True)).to_be_visible()
            expect(review.get_by_role("status")).to_contain_text(
                "MJobs does not submit applications.")
            expect(review.get_by_role("button", name="Submit", exact=False)).to_have_count(0)
            expect(review.get_by_role("button", name="Continue", exact=True)).to_have_count(0)

            page.reload(wait_until="domcontentloaded")
            review = self._open_application(page)
            expect(review.get_by_text("Manual intervention required", exact=True)).to_be_visible()
            persisted = self.client.get(self.application_url).json()
            self.assertEqual(persisted["status"], "REVIEW")
            self.assertEqual(persisted["current_page_id"], "review")
            self.assertEqual(persisted["progress"]["experience"]["summary"],
                             "Synthetic experience answer for browser acceptance.")
            self.assertEqual(outside_requests, [])
            self.assertTrue(requests)
            self.assertFalse(any("submit" in path.lower() or path.startswith("/ats/")
                                 for _, path in requests), requests)
            self.assertEqual(self.client.get(self.application_url).status_code, 200)
            application = self.store.application(self.application_id)
            self.assertEqual(application["state"], "PREPARED")
            self.assertIsNone(application["confirmation"])
            self.assertEqual(self.store.count("application_attempts"), 0)
            browser.close()


if __name__ == "__main__":
    unittest.main()
