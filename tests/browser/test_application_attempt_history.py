"""Real Chromium mock execution and connected durable-history acceptance."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.applications.attempt_history import AttemptConflict, read_attempt_history
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class AttemptHistoryBrowserTests(unittest.TestCase):
    def test_browser_execution_replay_and_connected_history_survive_restart(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-attempt-browser-") as directory, fixture_server() as fixture:
            root = Path(directory)
            url = f"sqlite:///{(root / 'test.db').as_posix()}"
            store = Store(url)
            store.migrate(); store.bind_workspace_mode("FIXTURE"); store.save_profile(dict(PROFILE))
            discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            app_id = prepare(store, job["id"], "fixture", root)
            result = apply_mock(store, app_id, fixture, root, request_id="browser-request-1")
            self.assertEqual(result["state"], "DRY_RUN_COMPLETE")
            with patch("services.applications.mock_adapter.sync_playwright", side_effect=AssertionError("Replay opened a browser")):
                self.assertEqual(apply_mock(store, app_id, fixture, root,
                    request_id="browser-request-1")["state"], "DRY_RUN_COMPLETE")
            history = read_attempt_history(store, "fixture", app_id)
            self.assertEqual(len(history["attempts"]), 1)
            attempt = history["attempts"][0]
            self.assertEqual(attempt["current_step"], "VALIDATE")
            self.assertIsNone(attempt["confirmation"])
            self.assertTrue(attempt["finished_at"])
            store.engine.dispose()
            reopened = Store(url)
            self.assertEqual(read_attempt_history(reopened, "fixture", app_id), history)
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
                        self.fail("Isolated history API did not start")
                    time.sleep(0.02)
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
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    for _ in range(2):
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Applications", exact=False).click()
                        page.get_by_role("region", name="Application pipeline").get_by_role(
                            "button", name=job["title"], exact=False).click()
                        panel = page.get_by_role("region", name="Durable attempt history", exact=True)
                        expect(panel).to_contain_text(attempt["attempt_id"])
                        expect(panel).to_contain_text("DRY_RUN_COMPLETE")
                        expect(panel).to_contain_text("VALIDATE:COMPLETED")
                        expect(panel).to_contain_text(attempt["finished_at"])
                        expect(panel).to_contain_text("Frozen inputs: profile version")
                        panel.get_by_role("button", name="Refresh attempt history").click()
                        expect(panel).to_contain_text(attempt["attempt_id"])
                    response = context.request.get(f"{origin}/profiles/other/applications/{app_id}/attempt-history")
                    self.assertEqual(response.status, 404)
                    self.assertFalse(external)
                    context.close(); browser.close()
            finally:
                server.should_exit = True; thread.join(timeout=10); sock.close()
                reopened.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_failed_browser_phase_is_immutable_when_user_starts_a_new_run(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-attempt-failure-") as directory, fixture_server() as fixture:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'test.db').as_posix()}")
            try:
                store.migrate(); store.bind_workspace_mode("FIXTURE"); store.save_profile(dict(PROFILE))
                discover(store, fixture, "fixture")
                app_id = prepare(store, store.jobs("fixture")[0]["id"], "fixture", root)
                def fail_after_navigation():
                    if store.application(app_id)["state"] == "FILLING":
                        raise RuntimeError("PRIVATE_BROWSER_VALUE must not enter history")
                with self.assertRaisesRegex(RuntimeError, "PRIVATE_BROWSER_VALUE"):
                    apply_mock(store, app_id, fixture, root, request_id="failed-request", check=fail_after_navigation)
                first = read_attempt_history(store, "fixture", app_id)["attempts"][0]
                self.assertEqual(first["state"], "REVIEW_REQUIRED")
                self.assertEqual(first["steps"], ["NAVIGATE:FAILED"])
                self.assertTrue(first["finished_at"])
                self.assertEqual(first["error_code"], "APPLICATION_EXECUTION_FAILED")
                self.assertNotIn("PRIVATE_BROWSER_VALUE", str(first))
                with patch("services.applications.mock_adapter.sync_playwright", side_effect=AssertionError("Failed replay opened a browser")):
                    with self.assertRaisesRegex(AttemptConflict, "REVIEW_REQUIRED"):
                        apply_mock(store, app_id, fixture, root, request_id="failed-request")
                retry = apply_mock(store, app_id, fixture, root, request_id="explicit-new-request")
                self.assertEqual(retry["state"], "DRY_RUN_COMPLETE")
                later = read_attempt_history(store, "fixture", app_id)["attempts"]
                self.assertEqual(len(later), 2)
                self.assertEqual(next(row for row in later if row["attempt_id"] == first["attempt_id"]), first)
            finally:
                store.engine.dispose()
