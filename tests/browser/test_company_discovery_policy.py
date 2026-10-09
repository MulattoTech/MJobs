"""Connected browser proof for profile-scoped company discovery controls."""
from pathlib import Path
from fixtures.browser_ports import bound_browser_socket
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class CompanyDiscoveryPolicyBrowserTests(unittest.TestCase):
    def test_policy_saves_and_survives_reload_without_external_requests(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-company-policy-browser-") as directory:
            database = (Path(directory) / "browser.db").as_posix()
            store = Store(f"sqlite:///{database}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(PROFILE.copy())
            app = create_app(store, "http://127.0.0.1:1", ROOT,
                             "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated company-policy API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
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
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option(str(PROFILE["id"]))
                    page.get_by_role("button", name="Profile", exact=True).click()
                    enabled = page.get_by_label("Enable company discovery", exact=False)
                    expect(enabled).to_be_checked()
                    expect(page.get_by_label("Minimum match score for discovery")).to_have_value("60")
                    expect(page.get_by_label("Match score for priority discovery")).to_have_value("80")
                    expect(page.get_by_label("Maximum new company discoveries per hour")).to_have_value("12")
                    enabled.uncheck()
                    page.get_by_label("Minimum match score for discovery").fill("65")
                    page.get_by_label("Match score for priority discovery").fill("90")
                    page.get_by_label("Maximum new company discoveries per hour").fill("4")
                    with page.expect_response(lambda response: response.request.method == "PUT"
                                               and urlsplit(response.url).path == "/profiles"):
                        page.get_by_role("button", name="Save profile", exact=True).first.click()
                    expect(enabled).not_to_be_checked()
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Profile", exact=True).click()
                    expect(page.get_by_label("Enable company discovery", exact=False)).not_to_be_checked()
                    expect(page.get_by_label("Minimum match score for discovery")).to_have_value("65")
                    expect(page.get_by_label("Match score for priority discovery")).to_have_value("90")
                    expect(page.get_by_label("Maximum new company discoveries per hour")).to_have_value("4")
                    expect(page.get_by_text("This never submits applications.", exact=False)).to_be_visible()
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated company-policy API must stop")
