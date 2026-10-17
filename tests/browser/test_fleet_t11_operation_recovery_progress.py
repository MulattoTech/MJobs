"""Mounted Activity reads real durable recovery, not a helper-only UI record."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from tests.integration.test_fleet_t11_operation_recovery_progress import RecoveryProgressFixture
from workers.tasks import execute_operation


ROOT = Path(__file__).resolve().parents[2]
RESUME_COPY = "Recovery requests continuation from saved progress."


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class FleetT11OperationRecoveryProgressBrowserTests(unittest.TestCase):
    def test_actual_recovery_reload_privacy_stale_profile_and_later_completion(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-t11-recovery-ui-") as directory, \
                RecoveryProgressFixture(Path(directory)) as flow, fixture_server() as mock_origin:
            snapshot = {"operations": list(flow.read().values())}
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(flow.store, mock_origin, flow.root, "redis://127.0.0.1:1/0",
                             operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external, errors, writes = [], [], []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Owned recovery-progress API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    try:
                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.set_default_timeout(10000)
                        page.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("request", lambda request: writes.append(request.url)
                                if request.method not in {"GET", "HEAD", "OPTIONS"} else None)
                        progress_path = "/profiles/fixture/operations/progress"

                        def open_activity():
                            page.goto(origin, wait_until="domcontentloaded")
                            with page.expect_response(lambda response: urlsplit(response.url).path == progress_path):
                                page.get_by_role("button", name="Activity", exact=True).click()
                            return page.get_by_role("region", name="Saved operation progress", exact=True)

                        def assert_pending(panel):
                            resumed = panel.get_by_role("article").filter(has_text=RESUME_COPY)
                            expect(resumed).to_have_count(1)
                            expect(resumed.get_by_label("Operation status: Queued", exact=True)).to_be_visible()
                            expect(resumed).to_contain_text("Recovering")
                            expect(resumed).not_to_contain_text("100%")
                            self.assertEqual(resumed.locator("dt", has_text="Completed").count(), 0)
                            expect(panel.get_by_role("article").filter(has_text="A retry is planned.")).to_have_count(1)
                            text = panel.inner_text()
                            for value in ["NETWORK_ERROR", "PRIVATE_PARAMS_SENTINEL", "PRIVATE_ERROR_SENTINEL",
                                          "PRIVATE_OWNER_SENTINEL", "PRIVATE_FOREIGN_SENTINEL",
                                          "PRIVATE_TRANSPORT_SENTINEL", "resumed after an interruption"]:
                                self.assertNotIn(value, text)

                        panel = open_activity()
                        assert_pending(panel)
                        # A fresh HTTP page request and DB connection read the same
                        # durable intent, without starting or completing the worker.
                        flow.store.engine.dispose()
                        panel = open_activity()
                        assert_pending(panel)
                        public = page.request.get(origin + progress_path).json()
                        rows = {row["operation_id"]: row for row in public["operations"]}
                        self.assertEqual(rows[flow.resume_id]["state"], "QUEUED")
                        self.assertEqual(rows[flow.resume_id]["recovery_summary"], "RESUME")
                        self.assertEqual(rows[flow.retry_id]["recovery_summary"], "RETRY: NETWORK_ERROR")

                        # Deliver an older actual profile response after switching.
                        # The connected view must discard it rather than resurrect
                        # the previous profile's recovered operations.
                        held = []
                        context.route("**" + progress_path, lambda route: held.append(route), times=1)
                        panel.get_by_role("button", name="Refresh progress", exact=True).click()
                        expect(panel.get_by_text("Loading operations…", exact=True)).to_be_visible()
                        self.assertEqual(len(held), 1)
                        with page.expect_response(lambda response:
                                urlsplit(response.url).path == "/profiles/other/operations/progress"):
                            page.get_by_label("Active profile", exact=True).select_option("other")
                        expect(panel.get_by_role("article")).to_have_count(1)
                        with page.expect_response(lambda response: urlsplit(response.url).path == progress_path):
                            held[0].fulfill(status=200, content_type="application/json", body=json.dumps(snapshot))
                        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                        expect(panel.get_by_role("article")).to_have_count(1)
                        expect(panel).not_to_contain_text(RESUME_COPY)
                        expect(panel).not_to_contain_text("A retry is planned.")
                        with page.expect_response(lambda response: urlsplit(response.url).path == progress_path):
                            page.get_by_label("Active profile", exact=True).select_option("fixture")
                        assert_pending(panel)

                        # Only a later real fixture DISCOVER execution, independent
                        # of any Activity HTTP request, can report completion.
                        with patch.dict("os.environ", {"MJOBS_MOCK_ORIGIN": mock_origin}):
                            completed = execute_operation(flow.resume_id)
                        self.assertEqual(completed["status"], "COMPLETED", completed)
                        public = page.request.get(origin + progress_path).json()
                        rows = {row["operation_id"]: row for row in public["operations"]}
                        self.assertEqual(rows[flow.resume_id]["state"], "COMPLETED")
                        self.assertEqual(rows[flow.resume_id]["progress"], 100)
                        self.assertIsNotNone(rows[flow.resume_id]["completed_at"])
                        self.assertEqual(rows[flow.resume_id]["recovery_summary"], "NONE")
                        panel = open_activity()
                        finished = panel.get_by_role("article").filter(has_text="No recovery action is available.")
                        expect(finished.get_by_label("Operation status: Completed", exact=True)).to_be_visible()
                        expect(finished).to_contain_text("100%")
                        expect(panel).not_to_contain_text(RESUME_COPY)
                        self.assertEqual(writes, [])
                        self.assertEqual(external, [])
                        self.assertEqual(errors, [])
                    finally:
                        context.close()
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                self.assertFalse(thread.is_alive(), "Owned recovery-progress API did not stop")
