"""Connected settings import rejection, reload and valid retry in Chromium."""
import copy
import json
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
from services.discovery.search_profiles import save_search_profile
from services.discovery.scheduler import schedule_due_search_profiles
from services.discovery.sources.collect import configure_source
from services.settings_transfer import export_settings
from tests.integration.test_spine import PROFILE


class SettingsTransferRecoveryBrowserTests(unittest.TestCase):
    def test_invalid_import_preserves_settings_then_valid_file_imports(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-settings-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(dict(PROFILE))
            source_id = configure_source(store, "settings-browser-fixture", jitter_seconds=0)
            save_search_profile(store, {"id": "original", "profile_id": "fixture",
                                       "name": "Original search", "enabled": True,
                                       "source_ids": [source_id],
                                       "scheduler": {"enabled": True, "interval_seconds": 600}})
            before = export_settings(store, "fixture")
            valid = copy.deepcopy(before)
            valid["settings"]["search_profiles"][0]["name"] = "Imported search"
            valid["settings"]["source_configuration"][0]["search_profile_name"] = "Imported search"
            valid["settings"]["automation_rules"][0]["search_profile_name"] = "Imported search"
            invalid = copy.deepcopy(valid)
            invalid["settings"]["search_profiles"][0]["criteria"]["remote"] = []
            conflicting = copy.deepcopy(valid)
            conflicting["settings"]["automation_rules"][0]["interval_seconds"] = 1800
            source_conflict = copy.deepcopy(valid)
            source_conflict["settings"]["source_configuration"][0]["sources"][0]["concurrency"] = 2
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(create_app(
                store, "http://127.0.0.1:1", root, "redis://127.0.0.1:1/0",
                operation_mode="PERSONAL"), log_level="critical"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated settings API did not start")
                    time.sleep(.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.set_default_timeout(8000)

                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        page.route("**/*", local_only)
                        page.goto(origin, wait_until="domcontentloaded")
                        page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                        page.reload(wait_until="domcontentloaded")
                        expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                        page.get_by_role("button", name="Profile", exact=True).click()
                        panel = page.get_by_role("region", name="Settings transfer", exact=True)
                        with page.expect_download() as captured_download:
                            panel.get_by_role("button", name="Download settings JSON", exact=True).click()
                        downloaded = Path(captured_download.value.path()).read_text(encoding="utf-8")
                        self.assertEqual(json.loads(downloaded), before)
                        for payload, status in ((invalid, 422), (conflicting, 422),
                                                (source_conflict, 422), (valid, 200)):
                            panel.get_by_label("Choose a settings JSON file (maximum 1 MiB)").set_input_files({
                                "name": "settings.json", "mimeType": "application/json",
                                "buffer": json.dumps(payload).encode()})
                            expect(panel.get_by_label("Settings import preview")).to_contain_text(
                                "1 automation rules")
                            panel.get_by_role("checkbox").check()
                            with page.expect_response(lambda response: response.url.endswith(
                                    "/profiles/fixture/settings/import") and response.request.method == "POST") as captured:
                                panel.get_by_role("button", name="Confirm import", exact=True).click()
                            self.assertEqual(captured.value.status, status)
                            if status == 422:
                                expect(panel.get_by_role("alert")).to_contain_text("No settings were changed")
                                self.assertEqual(export_settings(store, "fixture"), before)
                                page.reload(wait_until="domcontentloaded")
                                page.get_by_role("button", name="Profile", exact=True).click()
                            else:
                                expect(panel.get_by_role("status")).to_contain_text("Settings imported successfully")
                                self.assertEqual(export_settings(store, "fixture"), valid)
                        self.assertEqual(external, [])
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated settings API leaked")
            reopened = Store(database_url)
            try:
                self.assertEqual(export_settings(reopened, "fixture"), valid)
                scheduled = schedule_due_search_profiles(reopened)
                self.assertEqual(len(scheduled["scheduled"]), 1)
                self.assertEqual(schedule_due_search_profiles(reopened)["scheduled"], [])
            finally:
                reopened.engine.dispose()
