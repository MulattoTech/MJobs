"""A local browser can save, dismiss, annotate, and reload a job safely."""
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
from services.discovery.manual_capture import capture_manual_job
from tests.integration.test_spine import PROFILE


class JobTrackingBrowserTests(unittest.TestCase):
    def test_save_hide_notes_and_history_survive_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-tracking-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'trial.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            captured = capture_manual_job(store, {
                "request_id": "synthetic-tracking-role",
                "title": "Systems Administrator",
                "company": "Example Infrastructure",
                "description": "Maintain Windows Server and Active Directory.",
                "location": "Tulsa, OK",
                "remote_type": "UNKNOWN",
                "reference_url": "https://jobs.example.invalid/role/42",
                "application_url": "https://apply.example.invalid/role/42",
                "source_details": {"synthetic": True},
            })
            job_id = captured["job"]["id"]
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
                        self.fail("Isolated tracking API did not start")
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
                    lost_note_response = {"pending": True}

                    def drop_first_note_response(route):
                        response = route.fetch()
                        if lost_note_response["pending"]:
                            lost_note_response["pending"] = False
                            route.abort()
                        else:
                            route.fulfill(response=response)

                    page.route("**/tracking/notes", drop_first_note_response)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_text("External submission OFF", exact=True)).to_be_visible()
                    card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(card).to_be_visible()
                    card.click()
                    detail = page.locator(".job-detail")
                    expect(detail.get_by_role("region", name="Job tracking")).to_contain_text("ACTIVE")
                    detail.get_by_role("button", name="Save job", exact=True).click()
                    expect(detail.get_by_text("Current status:")).to_contain_text("SAVED")
                    page.get_by_label("Job note", exact=True).fill("Ask about the on-call rotation.")
                    page.get_by_role("button", name="Add note", exact=True).click()
                    expect(detail.get_by_role("list", name="Job tracking history").get_by_text(
                        "Ask about the on-call rotation.", exact=True)).to_have_count(0)
                    page.get_by_role("button", name="Add note", exact=True).click()
                    expect(detail).to_contain_text("Ask about the on-call rotation.")
                    expect(detail.get_by_role("list", name="Job tracking history").get_by_text(
                        "Ask about the on-call rotation.", exact=True)).to_have_count(1)
                    detail.get_by_role("button", name="Hide job", exact=True).click()
                    expect(detail.get_by_text("Current status:")).to_contain_text("HIDDEN")
                    expect(page.locator(".job-card").filter(has_text="Systems Administrator")).to_have_count(0)

                    page.reload(wait_until="domcontentloaded")
                    expect(page.locator(".job-card").filter(has_text="Systems Administrator")).to_have_count(0)
                    page.get_by_label("Show hidden and not interested", exact=True).check()
                    card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(card).to_be_visible()
                    card.click()
                    detail = page.locator(".job-detail")
                    expect(detail).to_contain_text("Current status:")
                    expect(detail).to_contain_text("HIDDEN")
                    expect(detail).to_contain_text("Ask about the on-call rotation.")
                    expect(detail.get_by_role("list", name="Job tracking history")).to_contain_text("SAVED")
                    expect(detail.get_by_role("list", name="Job tracking history")).to_contain_text("HIDDEN")
                    detail.get_by_role("button", name="Restore hidden job", exact=True).click()
                    expect(detail.get_by_text("Current status:")).to_contain_text("ACTIVE")
                    detail.get_by_role("button", name="Not interested", exact=True).click()
                    expect(detail.get_by_text("Current status:")).to_contain_text("NOT_INTERESTED")
                    page.get_by_label("Show hidden and not interested", exact=True).uncheck()
                    expect(page.locator(".job-card").filter(has_text="Systems Administrator")).to_have_count(0)
                    page.get_by_label("Show hidden and not interested", exact=True).check()
                    card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(card).to_be_visible()
                    card.click()
                    detail = page.locator(".job-detail")
                    expect(detail.get_by_text("Current status:")).to_contain_text("NOT_INTERESTED")
                    detail.get_by_role("button", name="Clear not interested", exact=True).click()
                    expect(detail.get_by_text("Current status:")).to_contain_text("ACTIVE")
                    self.assertEqual(external, [])
                    self.assertEqual(store.count("applications"), 0)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
