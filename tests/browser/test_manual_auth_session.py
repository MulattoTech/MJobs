"""The browser offers user-started sign-in and keeps rerun as a separate action."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ManualAuthSessionBrowserTests(unittest.TestCase):
    def test_sign_in_window_close_does_not_retry_until_user_selects_separate_rerun(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-manual-auth-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
                job = store.jobs("fixture")[0]
                application_id = prepare(store, job["id"], "fixture", root)
                with store.engine.begin() as connection:
                    route_id = store.application(application_id)["route_id"]
                    route = connection.execute(m.routes.select().where(
                        m.routes.c.id == route_id)).mappings().one()
                    connection.execute(m.routes.update().where(
                        m.routes.c.id == route_id).values(
                            url=f"{fixture}/ats/apply/{route['requisition']}?auth_expired=true"))
                paused = apply_mock(store, application_id, fixture, root, submit=True)
                self.assertEqual(paused["state"], "AUTH_REQUIRED")

                app = create_app(store, fixture, root,
                                 "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
                thread = threading.Thread(target=server.run,
                                          kwargs={"sockets": [sock]}, daemon=True)
                manual_requests = []
                rerun_requests = []

                def record_request(request):
                    path = urlsplit(request.url).path
                    if path.endswith("/manual-auth"):
                        manual_requests.append(request)
                    if path.endswith("/mock-run"):
                        rerun_requests.append(request)

                with patch("apps.api.manual_auth.open_mock_authentication",
                           return_value="MANUAL_AUTH_WINDOW_CLOSED") as open_window:
                    thread.start()
                    try:
                        deadline = time.monotonic() + 10
                        while not server.started:
                            if not thread.is_alive() or time.monotonic() > deadline:
                                self.fail("Isolated manual-auth API did not start")
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
                            page.set_default_timeout(8000)
                            page.on("request", record_request)
                            page.goto(origin, wait_until="domcontentloaded")
                            page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                            page.reload(wait_until="domcontentloaded")
                            page.get_by_role("button", name="Applications", exact=False).click()
                            board = page.get_by_role("region", name="Application pipeline")
                            card = board.get_by_role("region", name="Not tracked", exact=True).get_by_role(
                                "button", name=job["title"], exact=False)
                            expect(card).to_be_visible()
                            card.click()

                            auth_panel = page.get_by_role("region", name="Manual MOCK authentication")
                            expect(auth_panel).to_contain_text("AUTH_REQUIRED")
                            expect(auth_panel).to_contain_text("does not automatically rerun")
                            expect(auth_panel).to_contain_text("does not collect or display credentials")
                            attempts_before = store.count("application_attempts")
                            with page.expect_response(lambda response:
                                    response.url.endswith("/manual-auth") and response.status == 200):
                                auth_panel.get_by_role(
                                    "button", name="Open MOCK sign-in", exact=True).click()

                            open_window.assert_called_once()
                            self.assertEqual(len(manual_requests), 1)
                            self.assertEqual(rerun_requests, [])
                            self.assertEqual(store.count("application_attempts"), attempts_before)
                            self.assertEqual(store.application(application_id)["state"], "AUTH_REQUIRED")
                            self.assertNotIn("mock_session", page.locator("body").inner_text())
                            expect(auth_panel).to_be_visible()

                            # The only rerun request follows this distinct user action.
                            with page.expect_response(lambda response:
                                    response.url.endswith("/mock-run") and response.status == 202):
                                auth_panel.get_by_role(
                                    "button", name="Rerun MOCK application", exact=True).click()
                            self.assertEqual(len(rerun_requests), 1)
                            self.assertEqual(rerun_requests[0].post_data_json, {"submit": False})
                            self.assertEqual(store.application(application_id)["state"], "AUTH_REQUIRED")
                            self.assertFalse(external)
                            context.close()
                            browser.close()
                    finally:
                        server.should_exit = True
                        thread.join(timeout=10)
                        sock.close()
                        store.engine.dispose()
                        self.assertFalse(thread.is_alive())
