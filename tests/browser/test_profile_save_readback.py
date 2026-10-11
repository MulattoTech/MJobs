"""Saved-profile selection must not wait for the aggregate profile list."""

import asyncio
import copy
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


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ProfileSaveReadbackBrowserTests(unittest.TestCase):
    def test_saved_notice_has_selected_readback_during_delayed_list_refresh(self):
        self._exercise_save_readback(stale_initial_list=False)

    def test_older_profile_list_cannot_replace_saved_readback(self):
        self._exercise_save_readback(stale_initial_list=True)

    def _exercise_save_readback(self, *, stale_initial_list):
        with tempfile.TemporaryDirectory(prefix="mjobs-profile-save-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'profiles.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            other = copy.deepcopy(PROFILE)
            other.update(id="saved-profile-other", name="Synthetic other profile")
            store.save_profile(other)
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, origin, root, "redis://127.0.0.1:1/0",
                             operation_mode="FIXTURE")
            hold_list = threading.Event()
            list_waiting = threading.Event()
            hold_initial_list = threading.Event()
            initial_list_waiting = threading.Event()
            first_list = True
            if stale_initial_list:
                hold_initial_list.set()

            @app.middleware("http")
            async def delayed_list(request, call_next):
                nonlocal first_list
                if request.method == "GET" and request.url.path == "/profiles" and first_list:
                    first_list = False
                    if stale_initial_list:
                        # Snapshot before the PUT, then return this response last.
                        response = await call_next(request)
                        response.headers["x-fixture-stale-list"] = "true"
                        initial_list_waiting.set()
                        while hold_initial_list.is_set():
                            await asyncio.sleep(0.02)
                        return response
                if request.method == "GET" and request.url.path == "/profiles" and hold_list.is_set():
                    list_waiting.set()
                    while hold_list.is_set():
                        await asyncio.sleep(0.02)
                return await call_next(request)

            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external_requests = []
            page_errors = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated profile API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    selector = page.get_by_label("Active profile", exact=True)
                    if stale_initial_list:
                        deadline = time.monotonic() + 3
                        while not initial_list_waiting.is_set() and time.monotonic() < deadline:
                            page.wait_for_timeout(20)
                        self.assertTrue(initial_list_waiting.is_set())
                    else:
                        expect(selector.locator("option", has_text=other["name"])).to_have_count(1)
                    page.get_by_role("button", name="New profile", exact=True).click()
                    page.get_by_label("Full name", exact=True).fill("Synthetic saved profile")
                    page.get_by_label("Email", exact=True).fill("saved@example.invalid")
                    hold_list.set()
                    with page.expect_response(lambda response: response.request.method == "PUT"
                                              and response.url == origin + "/profiles") as saved_response:
                        page.get_by_role("button", name="Save profile", exact=True).click()
                    self.assertEqual(saved_response.value.status, 200)
                    saved_id = saved_response.value.json()["id"]
                    expect(page.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()
                    self.assertTrue(list_waiting.wait(3), "The aggregate list must still be pending")
                    self.assertEqual(selector.input_value(), saved_id)
                    self.assertEqual(selector.locator("option:checked").inner_text(), "Synthetic saved profile")
                    self.assertEqual(store.profile(saved_id)["email"], "saved@example.invalid")
                    with page.expect_response(lambda response:
                            response.request.method == "GET" and response.url == origin + "/profiles"
                            and response.headers.get("x-fixture-stale-list") != "true"):
                        hold_list.clear()
                    expect(selector).to_be_enabled()
                    if stale_initial_list:
                        with page.expect_response(lambda response:
                                response.headers.get("x-fixture-stale-list") == "true"):
                            hold_initial_list.clear()
                        # The old aggregate refresh applies its state after fetch resolves.
                        page.wait_for_timeout(100)
                        self.assertEqual(selector.input_value(), saved_id)
                        self.assertEqual(selector.locator("option:checked").inner_text(), "Synthetic saved profile")
                    page.reload(wait_until="domcontentloaded")
                    expect(selector).to_have_value(saved_id)
                    page.get_by_role("button", name="Profile", exact=True).click()
                    expect(page.get_by_label("Full name", exact=True)).to_have_value("Synthetic saved profile")
                    # An edit also replaces the existing label without duplicate options.
                    page.get_by_label("Full name", exact=True).fill("Synthetic renamed profile")
                    list_waiting.clear()
                    hold_list.set()
                    page.get_by_role("button", name="Save profile", exact=True).click()
                    expect(page.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()
                    self.assertTrue(list_waiting.wait(3))
                    self.assertEqual(selector.input_value(), saved_id)
                    self.assertEqual(selector.locator("option:checked").inner_text(), "Synthetic renamed profile")
                    self.assertEqual(selector.locator(f'option[value="{saved_id}"]').count(), 1)
                    hold_list.clear()
                    expect(selector).to_be_enabled()
                    selector.select_option(other["id"])
                    expect(page.get_by_label("Full name", exact=True)).to_have_value(other["name"])
                    self.assertEqual(store.profile(other["id"])["name"], other["name"])
                    selector.select_option(saved_id)
                    expect(page.get_by_label("Full name", exact=True)).to_have_value("Synthetic renamed profile")
                    self.assertEqual(external_requests, [])
                    self.assertEqual(page_errors, [])
                    context.close()
                    browser.close()
            finally:
                hold_list.clear()
                hold_initial_list.clear()
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
