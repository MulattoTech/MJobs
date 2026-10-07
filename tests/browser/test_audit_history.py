"""The Activity tab reads safe audit summaries for only the selected profile."""
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


class AuditHistoryBrowserTests(unittest.TestCase):
    def test_activity_history_is_profile_scoped_and_survives_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-audit-history-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Synthetic Applicant",
                                "email": "other@example.invalid"})
            with store.engine.begin() as connection:
                connection.execute(m.audit.insert(), [
                    {"id": "fixture-history", "entity_id": "fixture", "action": "SETTING_CHANGED",
                     "payload": {"value": "private-setting-value"},
                     "timestamp": "2026-10-07T10:00:00+00:00"},
                    {"id": "other-history", "entity_id": "other", "action": "PROFILE_SAVED",
                     "payload": {"value": "other-private-setting"},
                     "timestamp": "2026-10-07T10:01:00+00:00"},
                ])
            app = create_app(store, "http://127.0.0.1:1", ROOT,
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
                        self.fail("Isolated audit-history API did not start")
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
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    history = page.get_by_role("list", name="Audit history")
                    expect(history).to_contain_text("A profile setting changed")
                    expect(history).not_to_contain_text("private-setting-value")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    expect(page.get_by_role("list", name="Audit history")).to_contain_text(
                        "A profile setting changed")
                    expect(page.get_by_role("list", name="Audit history")).not_to_contain_text(
                        "other-private-setting")
                    page.get_by_role("button", name="Profile", exact=True).click()
                    page.get_by_label("Active profile", exact=True).select_option("other")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    other_history = page.get_by_role("list", name="Audit history")
                    expect(other_history).to_contain_text("Profile details updated")
                    expect(other_history).not_to_contain_text("A profile setting changed")
                    expect(other_history).not_to_contain_text("fixture-history")
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()
