"""Real UI/API source enrollment; no provider network calls or personal data."""
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


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web first")
class SourceEnrollmentBrowserTests(unittest.TestCase):
    def test_aggregator_enrollment_reload_and_toggle_preserve_provider(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-source-enrollment-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'test.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Controlled API startup failed")
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()
                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    page.get_by_label("Source provider", exact=True).select_option("remotive")
                    expect(page.get_by_label("Board token", exact=True)).to_be_hidden()
                    with page.expect_response(lambda r: urlsplit(r.url).path == "/sources" and r.request.method == "PUT") as saved:
                        page.get_by_role("button", name="Add source", exact=True).click()
                    self.assertEqual(saved.value.status, 200)
                    rows = page.request.get(origin + "/sources").json()["sources"]
                    self.assertEqual([(r["provider"], r["tenant"]) for r in rows], [("remotive", "default")])
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    panel = page.locator("section.panel").filter(has=page.get_by_role("heading", name=rows[0]["name"], exact=True))
                    with page.expect_response(lambda r: urlsplit(r.url).path == "/sources" and r.request.method == "PUT"):
                        panel.get_by_role("button", name="Disable", exact=True).click()
                    rows = page.request.get(origin + "/sources").json()["sources"]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["provider"], "remotive")
                    self.assertFalse(rows[0]["enabled"])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
