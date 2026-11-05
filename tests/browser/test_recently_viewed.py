"""Connected synthetic recent-history journey; no live browser profile or external requests."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import func, select, update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.tracking.recently_viewed import list_recent_job_views
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class RecentlyViewedBrowserTests(unittest.TestCase):
    def test_explicit_visits_reload_closed_status_profile_isolation_and_clear(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-recent-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'trial.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Synthetic Applicant",
                                "email": "other@example.invalid"})
            jobs = []
            for index, title in enumerate(("Systems Administrator", "Network Engineer")):
                captured = capture_manual_job(store, {
                    "request_id": f"recent-browser-{index}",
                    "title": title, "company": "Synthetic Employer",
                    "description": "Maintain Windows Server and Active Directory.",
                    "location": "Remote", "remote_type": "UNKNOWN",
                    "reference_url": f"https://jobs.example.invalid/recent/{index}",
                    "application_url": f"https://apply.example.invalid/recent/{index}",
                    "source_details": {"synthetic": True},
                })
                jobs.append(captured["job"]["id"])
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated recent-history API did not start")
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    try:
                        page.goto(origin, wait_until="domcontentloaded")
                        page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                        page.reload(wait_until="domcontentloaded")
                        expect(page.get_by_text("External submission OFF", exact=True)).to_be_visible()
                        panel = page.get_by_role("region", name="Recently viewed jobs")
                        expect(panel).to_contain_text("No jobs viewed in this profile yet.")
                        self.assertEqual(list_recent_job_views(store, "fixture")["items"], [])

                        def open_card(title):
                            with page.expect_response(
                                lambda response: response.request.method == "POST" and
                                response.url.endswith("/view")
                            ) as receipt:
                                page.locator(".job-card").filter(has_text=title).click()
                            self.assertEqual(receipt.value.status, 201)

                        open_card("Systems Administrator")
                        expect(panel.get_by_role("listitem")).to_have_count(1)
                        open_card("Systems Administrator")
                        expect(panel.get_by_role("listitem")).to_have_count(1)
                        open_card("Network Engineer")
                        expect(panel.get_by_role("listitem")).to_have_count(2)
                        expect(panel.get_by_role("listitem").first).to_contain_text("Network Engineer")
                        page.get_by_label("Active profile", exact=True).select_option("other")
                        expect(panel).to_contain_text("No jobs viewed in this profile yet.")
                        open_card("Systems Administrator")
                        expect(panel.get_by_role("listitem")).to_have_count(1)
                        page.get_by_label("Active profile", exact=True).select_option("fixture")
                        expect(panel.get_by_role("listitem")).to_have_count(2)

                        with store.engine.begin() as connection:
                            attributes = connection.execute(select(m.jobs.c.attributes).where(
                                m.jobs.c.id == jobs[1])).scalar_one()
                            connection.execute(update(m.jobs).where(m.jobs.c.id == jobs[1]).values(
                                attributes={**attributes, "status": "CLOSED"}))
                        panel.get_by_role("button", name="Refresh visits", exact=True).click()
                        expect(panel).to_contain_text("No longer open")
                        expect(panel.get_by_role("listitem").first).to_contain_text("Network Engineer")
                        expect(panel.get_by_role("button", name="Network Engineer", exact=False)).to_have_count(0)
                        expect(panel.get_by_role("button", name="Systems Administrator", exact=False)).to_have_count(1)
                        page.reload(wait_until="domcontentloaded")
                        expect(panel).to_contain_text("No longer open")
                        expect(panel.get_by_role("listitem")).to_have_count(2)
                        page.set_viewport_size({"width": 390, "height": 844})
                        expect(panel).to_be_visible()
                        expect(panel).to_contain_text("Saved-only filtering is unavailable")

                        page.once("dialog", lambda dialog: dialog.dismiss())
                        panel.get_by_role("button", name="Clear visit history", exact=True).click()
                        expect(panel.get_by_role("listitem")).to_have_count(2)
                        self.assertEqual(len(list_recent_job_views(store, "fixture")["items"]), 2)
                        page.once("dialog", lambda dialog: dialog.accept())
                        panel.get_by_role("button", name="Clear visit history", exact=True).click()
                        expect(panel).to_contain_text("No jobs viewed in this profile yet.")
                        page.get_by_label("Active profile", exact=True).select_option("other")
                        expect(panel.get_by_role("listitem")).to_have_count(1)
                        page.get_by_label("Active profile", exact=True).select_option("fixture")
                        page.reload(wait_until="domcontentloaded")
                        expect(panel).to_contain_text("No jobs viewed in this profile yet.")
                        with store.engine.connect() as connection:
                            self.assertEqual(connection.execute(select(func.count()).select_from(
                                m.applications)).scalar_one(), 0)
                            self.assertEqual(connection.execute(select(func.count()).select_from(m.audit).where(
                                m.audit.c.action.in_(("JOB_DISPOSITION", "JOB_NOTE")))).scalar_one(), 0)
                        self.assertFalse(external)
                    finally:
                        context.close()
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
