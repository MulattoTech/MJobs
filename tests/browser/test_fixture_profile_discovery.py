"""Saved-profile fixture discovery is explicit and unavailable in Personal mode."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import uvicorn
from playwright.sync_api import expect, sync_playwright

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build web first")
class FixtureProfileDiscoveryBrowserTests(unittest.TestCase):
    def test_collects_for_saved_profile_without_replacing_it_and_is_personal_disabled(self):
        for mode in ("FIXTURE", "PERSONAL"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, fixture_server() as mock_origin:
                root = Path(directory)
                database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
                store = Store(database_url)
                store.migrate()
                store.bind_workspace_mode(mode)
                profile_id = "saved-fixture-review-profile"
                store.save_profile({**PROFILE, "id": profile_id})
                original = store.profile(profile_id)
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(create_app(
                    store, mock_origin, root, "redis://127.0.0.1:1/0", operation_mode=mode), log_level="error"))
                thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
                try:
                    thread.start()
                    deadline = time.monotonic() + 10
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            self.fail("Controlled API did not start")
                        time.sleep(.02)
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        context = browser.new_context()
                        page = context.new_page()
                        page.set_default_timeout(8000)
                        page.goto(origin)
                        page.get_by_label("Active profile", exact=True).select_option(profile_id)
                        page.get_by_role("navigation").get_by_role("button", name="Sources", exact=True).click()
                        button = page.get_by_role("button", name="Collect local fixture jobs", exact=True)
                        if mode == "PERSONAL":
                            expect(button).to_have_count(0)
                            self.assertEqual(context.request.post(origin + "/discovery", data={
                                "profile_id": profile_id, "request_id": "personal-denied"}).status, 409)
                            self.assertEqual(store.count("worker_operations"), 0)
                        else:
                            expect(button).to_be_enabled(timeout=500)
                            operation_ids = []
                            def execute(route):
                                response = route.fetch()
                                self.assertEqual(response.status, 202)
                                self.assertEqual(route.request.post_data_json["profile_id"], profile_id)
                                operation_id = response.json()["operation_id"]
                                operation_ids.append(operation_id)
                                with patch.dict("os.environ", MJOBS_DATABASE_URL=database_url,
                                                MJOBS_OPERATION_MODE=mode, MJOBS_OUTPUT_DIR=str(root),
                                                MJOBS_MOCK_ORIGIN=mock_origin):
                                    from workers.tasks import execute_operation
                                    self.assertEqual(execute_operation(operation_id)["status"], "COMPLETED")
                                route.fulfill(response=response)
                            context.route("**/discovery", execute)
                            button.click()
                            expect(page.get_by_role("status").filter(has_text="Local fixture jobs collected")).to_be_visible()
                            expect(page.get_by_label("Active profile", exact=True)).to_have_value(profile_id)
                            self.assertEqual(len(operation_ids), 1)
                            self.assertEqual(context.request.get(origin + "/operations/" + operation_ids[0]).json()["entity_id"], profile_id)
                            self.assertGreater(store.count("canonical_jobs"), 0)
                            self.assertEqual(len(store.profiles()), 1)
                            self.assertEqual(store.profile(profile_id), original)
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(timeout=10)
                    sock.close()
                    store.engine.dispose()
                    self.assertFalse(thread.is_alive())
