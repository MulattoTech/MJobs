"""Connected browser acceptance for persisted company preferences and recovery."""
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class CompanyPreferencesConnectedBrowserTests(unittest.TestCase):
    def test_blacklist_is_saved_across_reload_and_can_be_cleared_from_search(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-company-preferences-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'preferences.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            job = capture_manual_job(store, {
                "request_id": "company-preference-browser-job",
                "title": "Synthetic Company Preference Role",
                "company": "Synthetic Preference Employer",
                "description": "Fictional systems role for preference workflow validation.",
                "reference_url": "https://jobs.example.test/company-preference",
                "application_url": "https://apply.example.test/company-preference",
                "profile_id": "fixture",
            })["job"]
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(
                app, log_level="error", timeout_graceful_shutdown=10))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external_requests = []
            try:
                thread.start()
                from time import monotonic, sleep
                deadline = monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or monotonic() > deadline:
                        self.fail("Isolated company-preferences API did not start")
                    sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.goto(origin, wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    card = page.locator(".job-list .job-card").filter(
                        has_text="Synthetic Company Preference Role")
                    expect(card).to_have_count(1)
                    card.click()
                    preference = page.get_by_role("region", name="Company preference", exact=True)
                    expect(preference).to_contain_text(
                        "No explicit choice has been saved; the current default is neutral.")

                    preference.get_by_role("radio", name=re.compile("Avoid this company")).check()
                    with page.expect_response(lambda response:
                            response.request.method == "PUT" and
                            urlsplit(response.url).path.endswith(
                                f"/company-preferences/{job['company_id']}")) as saved_response:
                        preference.get_by_role(
                            "button", name="Save company preference", exact=True).click()
                    self.assertEqual(saved_response.value.status, 200)
                    expect(preference).to_contain_text("Preference saved and read back")
                    expect(card).to_have_count(0)

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    expect(page.locator(".job-list .job-card")).to_have_count(0)
                    page.get_by_label("Show blacklisted companies", exact=True).check()
                    restored_card = page.locator(".job-list .job-card").filter(
                        has_text="Synthetic Company Preference Role")
                    expect(restored_card).to_have_count(1)
                    restored_card.click()
                    preference = page.get_by_role("region", name="Company preference", exact=True)
                    expect(preference).to_contain_text("Saved preference: BLACKLISTED")

                    preference.get_by_role("radio", name=re.compile("No preference")).check()
                    with page.expect_response(lambda response:
                            response.request.method == "PUT" and
                            urlsplit(response.url).path.endswith(
                                f"/company-preferences/{job['company_id']}")) as cleared_response:
                        preference.get_by_role(
                            "button", name="Save company preference", exact=True).click()
                    self.assertEqual(cleared_response.value.status, 200)
                    expect(restored_card).to_have_count(1)
                    confirmed = page.request.get(
                        origin + f"/profiles/fixture/company-preferences/{job['company_id']}")
                    self.assertEqual(confirmed.status, 200, confirmed.text())
                    self.assertEqual(confirmed.json()["status"], "NEUTRAL")
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()
            self.assertEqual(external_requests, [])
