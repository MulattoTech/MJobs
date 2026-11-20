"""Real source enrollment and visible provider credit; no upstream requests."""
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
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web first")
class USAJOBSSourceEnrollmentBrowserTests(unittest.TestCase):
    def test_enrollment_credit_reload_and_disable_use_the_real_api(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-usajobs-enrollment-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'test.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error",
            ))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external, page_errors = [], []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated USAJOBS enrollment API did not start")
                    time.sleep(0.02)
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
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    selector = page.get_by_label("Source provider", exact=True)
                    expect(selector.locator('option[value="usajobs"]')).to_have_text("USAJOBS (recent federal IT)")
                    selector.select_option("usajobs")
                    expect(page.get_by_label("Board token", exact=True)).to_be_hidden()
                    expect(page.get_by_label("Lever site", exact=True)).to_be_hidden()
                    page.get_by_label("Interval (minutes)", exact=True).fill("60")
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == "/sources"
                            and response.request.method == "PUT") as saved:
                        page.get_by_role("button", name="Add source", exact=True).click()
                    self.assertEqual(saved.value.status, 200)

                    sources = page.request.get(origin + "/sources").json()["sources"]
                    self.assertEqual(len(sources), 1)
                    source = sources[0]
                    self.assertEqual((source["provider"], source["tenant"]), ("usajobs", "default"))
                    self.assertEqual(source["poll_interval"], 3600)
                    self.assertEqual(source["strategy"], "PUBLIC_JSON")
                    self.assertEqual(set(source["capabilities"]["capabilities"]),
                                     {"DISCOVERY", "APPLICATION_REDIRECT"})
                    self.assertFalse(source["capabilities"]["direct_application"])
                    self.assertFalse(source["capabilities"]["application_status"])
                    self.assertFalse(source["capabilities"]["salary"])
                    self.assertNotIn("REMOTE_METADATA", source["capabilities"]["capabilities"])
                    self.assertFalse(source["capabilities"]["job_detail"])

                    # Source credit remains visible after the form resets/reloads.
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    credit = page.get_by_role("link", name="USAJOBS federal announcements", exact=True).first
                    expect(credit).to_be_visible()
                    expect(credit).to_have_attribute("href", "https://www.usajobs.gov/")
                    panel = page.locator("section.panel").filter(
                        has=page.get_by_role("heading", name=source["name"], exact=True))
                    expect(panel).to_contain_text("up to 25 announcements from a seven-day opening window")
                    expect(panel).to_contain_text("Coverage is incomplete")
                    expect(panel).to_contain_text("Telework eligibility does not establish fully remote work")
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == "/sources"
                            and response.request.method == "PUT") as disabled:
                        panel.get_by_role("button", name="Disable", exact=True).click()
                    self.assertEqual(disabled.value.status, 200)
                    rows = page.request.get(origin + "/sources").json()["sources"]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["id"], source["id"])
                    self.assertEqual(rows[0]["provider"], "usajobs")
                    self.assertFalse(rows[0]["enabled"])
                    self.assertEqual(store.count(m.jobs.name), 0)
                    self.assertEqual(store.count(m.operations.name), 0)
                    self.assertEqual(store.count(m.applications.name), 0)
                    self.assertEqual(external, [])
                    self.assertEqual(page_errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
