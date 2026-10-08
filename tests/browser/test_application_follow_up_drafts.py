"""Connected local follow-up drafts are copy-only and survive browser reload."""
from datetime import datetime, timedelta, timezone
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
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.tracking.applications import set_application_pipeline_status
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationFollowUpDraftBrowserTests(unittest.TestCase):
    def test_interval_candidates_copy_only_and_reload_are_local_read_only(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-follow-up-browser-") as directory:
            root = Path(directory)
            database = root / "follow-up-browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Synthetic Applicant"})
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
                jobs = store.jobs("fixture")
                self.assertGreaterEqual(len(jobs), 2)
                eligible = prepare(store, jobs[0]["id"], "fixture", root)
                excluded = prepare(store, jobs[1]["id"], "fixture", root)
                set_application_pipeline_status(
                    store, "fixture", eligible, "APPLIED", "browser-follow-up-applied")
                set_application_pipeline_status(
                    store, "fixture", excluded, "SAVED", "browser-follow-up-saved")
                event_id = identity("application-tracking-event", "fixture", eligible,
                                    "browser-follow-up-applied")
                with store.engine.begin() as connection:
                    changed = connection.execute(m.audit.update().where(
                        m.audit.c.id == event_id).values(
                            timestamp=(datetime.now(timezone.utc)
                                       - timedelta(days=10)).isoformat()))
                self.assertEqual(changed.rowcount, 1)

                app = create_app(store, "http://127.0.0.1:1", root,
                                 "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
                thread = threading.Thread(target=server.run,
                                          kwargs={"sockets": [sock]}, daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 10
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            self.fail("Isolated follow-up API did not start")
                        time.sleep(0.02)

                    requests = []
                    external_requests = []
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])

                        def local_only(route):
                            request = route.request
                            requests.append((request.method, request.url))
                            if urlsplit(request.url).netloc != urlsplit(origin).netloc:
                                external_requests.append(request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.set_default_timeout(8000)
                        page.goto(origin, wait_until="domcontentloaded")
                        page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Applications", exact=True).click()

                        follow_up = page.get_by_role("region", name="Follow-up drafts")
                        expect(follow_up).to_be_visible()
                        follow_up.get_by_label("Quiet days before follow-up").select_option("10")
                        expect(follow_up).to_contain_text(jobs[0]["title"])
                        expect(follow_up).not_to_contain_text(jobs[1]["title"])
                        expect(follow_up).to_contain_text(
                            "MJobs has no evidence that the employer received your application")

                        draft_details = follow_up.locator("article dl dd")
                        email_body = draft_details.nth(2)
                        dm_body = draft_details.nth(4)
                        expect(email_body).to_be_visible()
                        expect(dm_body).to_be_visible()
                        email_text = email_body.inner_text()
                        dm_text = dm_body.inner_text()
                        self.assertLessEqual(len(email_text), 500)
                        self.assertLessEqual(len(dm_text), 280)

                        follow_up.get_by_role("button", name="Copy email body", exact=True).click()
                        copied_email = page.evaluate("navigator.clipboard.readText()")
                        self.assertEqual(copied_email, email_text)
                        follow_up.get_by_role("button", name="Copy DM wording", exact=True).click()
                        copied_dm = page.evaluate("navigator.clipboard.readText()")
                        self.assertEqual(copied_dm, dm_text)
                        self.assertEqual(follow_up.get_by_role(
                            "button", name="Send", exact=False).count(), 0)
                        self.assertEqual(follow_up.get_by_role(
                            "button", name="Submit", exact=False).count(), 0)

                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Applications", exact=True).click()
                        follow_up = page.get_by_role("region", name="Follow-up drafts")
                        expect(follow_up).to_contain_text(jobs[0]["title"])
                        expect(follow_up).not_to_contain_text(jobs[1]["title"])
                        self.assertEqual(external_requests, [])
                        self.assertTrue(requests)
                        self.assertTrue(all(method in {"GET", "HEAD"} for method, _ in requests),
                                        requests)
                        self.assertFalse(any(
                            any(term in url.lower() for term in ("/send", "/submit", "/apply"))
                            for _, url in requests))
                        browser.close()

                    self.assertEqual(store.count("applications"), 2)
                    self.assertEqual(store.count("worker_operations"), 0)
                    self.assertEqual(store.count("application_attempts"), 0)
                finally:
                    server.should_exit = True
                    thread.join(timeout=10)
                    store.engine.dispose()
                    self.assertFalse(thread.is_alive(), "Local follow-up API did not stop")
