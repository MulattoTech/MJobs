"""The application UI records a profile-scoped external report and reloads it."""
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
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class ExternalApplicationCaptureBrowserTests(unittest.TestCase):
    def test_capture_and_history_survive_reload_without_employer_traffic(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-external-application-browser-") as directory:
            root = Path(directory)
            database = root / "capture.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Synthetic Applicant",
                                "email": "other@example.invalid", "facts": [], "answers": []})
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
                        self.fail("Isolated external-application API did not start")
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    external_requests = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications").click()
                    page.get_by_label("External application company", exact=True).fill(
                        "Synthetic Referral Employer")
                    page.get_by_label("External application title", exact=True).fill(
                        "Systems Administrator")
                    page.get_by_label("External application URL", exact=True).fill(
                        "https://apply.example.invalid/synthetic-role")
                    page.get_by_label("External application source", exact=True).select_option("REFERRAL")
                    page.get_by_label("External application date and time", exact=True).fill(
                        "2026-10-03T09:30")
                    page.get_by_role("button", name="Record external application", exact=True).click()

                    card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(card).to_contain_text("Externally reported")
                    expect(card).to_contain_text("EXTERNALLY_APPLIED")
                    detail = page.locator(".job-detail")
                    expect(detail).to_contain_text("External application reported via REFERRAL")
                    expect(detail).to_contain_text("external user report")
                    self.assertEqual(store.count("applications"), 1)
                    self.assertEqual(store.count("worker_operations"), 0)

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications").click()
                    card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(card).to_contain_text("Externally reported")
                    card.click()
                    expect(page.locator(".job-detail")).to_contain_text(
                        "External application reported via REFERRAL")

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    page.get_by_role("button", name="Applications").click()
                    expect(page.locator(".job-card").filter(
                        has_text="Systems Administrator")).to_have_count(0)
                    self.assertEqual(external_requests, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
