"""Real API admission, Activity readback, worker completion and browser reload."""
from pathlib import Path
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from tests.integration import test_preparation_priority as priority_fixtures
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


@unittest.skipUnless((Path(__file__).resolve().parents[2] / "apps/web/dist/index.html").is_file(),
                     "Build the production frontend before browser acceptance")
class PreparationPriorityBrowserTests(unittest.TestCase):
    def test_priority_is_grounded_profile_scoped_and_survives_worker_and_page_restart(self):
        fixture = priority_fixtures.PreparationPriorityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.store.save_profile({**PROFILE, "id": "other", "name": "Other synthetic profile"})
        low = fixture.admit(fixture.job("browser-old", age=1000), "old")
        high_job = fixture.job("browser-fresh")
        high = fixture.admit(high_job, "fresh")
        fixture.admit(high_job, "other", "other")
        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(fixture.store, "http://127.0.0.1:8090", fixture.root, operation_mode="FIXTURE")
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        external = []
        try:
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    self.fail("Priority test API failed to start")
                time.sleep(.02)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()
                    page.route("**/*", local_only)
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload()
                    page.get_by_role("button", name="Activity", exact=True).click()
                    panel = page.get_by_role("region", name="Saved operation progress", exact=True)
                    expect(panel.get_by_role("article")).to_have_count(2)
                    expect(panel).to_contain_text("Fresh excellent match")
                    expect(panel).to_contain_text("Waiting work moves up one tier each hour")
                    self.assertEqual(execute_operation(high)["status"], "COMPLETED")
                    page.reload()
                    page.get_by_role("button", name="Activity", exact=True).click()
                    expect(panel).to_contain_text("Completed")
                    expect(panel).to_contain_text("Fresh excellent match")
                    page.get_by_label("Active profile", exact=True).select_option("other")
                    expect(panel.get_by_role("article")).to_have_count(1)
                    expect(panel).not_to_contain_text("Completed")
                    expect(panel).not_to_contain_text("Preparation priority: Low")
                    self.assertEqual(fixture.row(low)["attempt_count"], 0)
                    self.assertEqual(external, [])
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            self.assertFalse(thread.is_alive(), "Priority test API did not stop")
