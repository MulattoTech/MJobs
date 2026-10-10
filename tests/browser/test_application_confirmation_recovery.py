"""Real lost-confirmation/restart/UI recovery with exactly one mock submission."""
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import sync_playwright, expect
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.applications.attempt_history import read_attempt_history
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class ConfirmationRecoveryBrowserTests(unittest.TestCase):
    def test_lost_receipt_restart_and_connected_check_never_resubmit(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-receipt-recovery-") as directory, fixture_server() as fixture:
            root = Path(directory)
            url = f"sqlite:///{(root / 'test.db').as_posix()}"
            store = Store(url)
            store.migrate(); store.bind_workspace_mode("FIXTURE"); store.save_profile(dict(PROFILE))
            discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            app_id = prepare(store, job["id"], "fixture", root)
            # The fictional provider receives one submission; the caller cannot
            # verify its response and must preserve uncertainty across restart.
            with patch("services.applications.mock_adapter._receipt", return_value=None):
                with self.assertRaisesRegex(ValueError, "receipt missing or invalid"):
                    apply_mock(store, app_id, fixture, root, submit=True, request_id="lost-confirmation")
            self.assertEqual(store.application(app_id)["state"], "SUBMISSION_UNCONFIRMED")
            original_history = read_attempt_history(store, "fixture", app_id)
            self.assertEqual(original_history["attempts"][0]["state"], "SUBMISSION_UNCONFIRMED")
            self.assertIsNone(original_history["attempts"][0]["confirmation"])
            before = httpx.get(fixture + "/_test/request-counts", trust_env=False).json()
            self.assertEqual(before, {"review": 1, "submit": 1})
            store.engine.dispose()
            reopened = Store(url)
            app = create_app(reopened, fixture, root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated reconciliation API did not start")
                    time.sleep(0.02)
                with patch("services.applications.mock_adapter.sync_playwright", side_effect=AssertionError("Reconciliation must not launch a browser")):
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context()
                        external = []
                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url); route.abort()
                            else:
                                route.continue_()
                        context.route("**/*", local_only)
                        page = context.new_page(); page.set_default_timeout(8000)
                        requests = []
                        page.on("request", lambda request: requests.append(request.post_data_json)
                                if request.url.endswith("/confirmation-check") else None)
                        page.goto(origin, wait_until="domcontentloaded")
                        page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Applications", exact=False).click()
                        page.get_by_role("region", name="Application pipeline").get_by_role(
                            "button", name=job["title"], exact=False).click()
                        panel = page.get_by_role("region", name="Verify uncertain mock submission", exact=True)
                        expect(panel).to_be_visible()
                        panel.get_by_role("button", name="Check receipt only").click()
                        expect(panel).to_contain_text("Fixture receipt verified")
                        expect(panel).to_contain_text("No browser was launched")
                        self.assertEqual(reopened.application(app_id)["state"], "CONFIRMED")
                        self.assertEqual(len(requests), 1)
                        replay = context.request.post(f"{origin}/profiles/fixture/applications/{app_id}/confirmation-check", data=requests[0])
                        self.assertEqual(replay.status, 200)
                        self.assertTrue(replay.json()["replayed"])
                        self.assertFalse(replay.json()["submission_attempted"])
                        self.assertEqual(read_attempt_history(reopened, "fixture", app_id), original_history)
                        self.assertEqual(httpx.get(fixture + "/_test/request-counts", trust_env=False).json(), before)
                        reference = reopened.application(app_id)["confirmation"]["reference_number"]
                        expect(panel).to_contain_text(reference)
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Applications", exact=False).click()
                        page.get_by_role("region", name="Application pipeline").get_by_role(
                            "button", name=job["title"], exact=False).click()
                        expect(page.locator("pre").filter(has_text=reference)).to_be_visible()
                        self.assertFalse(external)
                        context.close(); browser.close()
            finally:
                server.should_exit = True; thread.join(timeout=10); sock.close()
                reopened.engine.dispose()
                self.assertFalse(thread.is_alive())
