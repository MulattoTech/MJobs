"""Connected browser proof for safe, durable company discovery activity."""
import copy
from pathlib import Path
from fixtures.browser_ports import bound_browser_socket
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import insert
import uvicorn

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class CompanyDiscoveryActivityBrowserTests(unittest.TestCase):
    def test_activity_is_visible_after_refresh_and_reload_and_is_profile_scoped(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-company-activity-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Synthetic Applicant",
                                "email": "other@example.invalid"})
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "browser-activity-acme", "name": "Acme Systems", "domain": "",
                })
                connection.execute(insert(m.audit), [
                    {
                        "id": "browser-activity-discovered", "entity_id": "fixture",
                        "action": "COMPANY_DISCOVERED", "timestamp": "2026-10-08T09:00:00+00:00",
                        "payload": {"company_id": "browser-activity-acme",
                                    "raw_payload": "SYNTHETIC_BROWSER_RAW_SECRET",
                                    "imported_count": None},
                    },
                    {
                        "id": "browser-activity-enrolled", "entity_id": "fixture",
                        "action": "COMPANY_SOURCE_ENROLLED", "timestamp": "2026-10-08T09:05:00+00:00",
                        "payload": {"company_id": "browser-activity-acme",
                                    "company_source_id": "synthetic-private-source-id",
                                    "raw_payload": "SYNTHETIC_BROWSER_RAW_SECRET"},
                    },
                    {
                        "id": "browser-activity-other-profile", "entity_id": "other",
                        "action": "COMPANY_DISCOVERED", "timestamp": "2026-10-08T09:10:00+00:00",
                        "payload": {"company_id": "browser-activity-acme",
                                    "raw_payload": "OTHER_PROFILE_BROWSER_SECRET"},
                    },
                ])
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
                        self.fail("Isolated company-activity API did not start")
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
                    page.locator("#active-profile").select_option("fixture")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    activity = page.get_by_role("list", name="Company discovery activity")
                    expect(activity).to_contain_text("Discovered Acme Systems")
                    expect(activity).to_contain_text("Added direct employer source")
                    expect(activity).not_to_contain_text("SYNTHETIC_BROWSER_RAW_SECRET")
                    expect(activity).not_to_contain_text("synthetic-private-source-id")
                    expect(activity).not_to_contain_text("unknown")

                    with page.expect_response(lambda response: response.request.method == "GET"
                                               and response.url.endswith("/company-discovery/activity")):
                        page.get_by_role("button", name="Refresh activity", exact=True).click()
                    expect(activity).to_contain_text("Discovered Acme Systems")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    reloaded = page.get_by_role("list", name="Company discovery activity")
                    expect(reloaded).to_contain_text("Discovered Acme Systems")
                    expect(reloaded).to_contain_text("Added direct employer source")
                    expect(reloaded).not_to_contain_text("OTHER_PROFILE_BROWSER_SECRET")

                    page.get_by_role("button", name="Profile", exact=True).click()
                    page.get_by_label("Active profile", exact=True).select_option("other")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    other_activity = page.get_by_role("list", name="Company discovery activity")
                    expect(other_activity).to_contain_text("Discovered Acme Systems")
                    expect(other_activity).not_to_contain_text("Added direct employer source")
                    expect(other_activity).not_to_contain_text("SYNTHETIC_BROWSER_RAW_SECRET")
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated company-activity API must stop")


if __name__ == "__main__":
    unittest.main()
