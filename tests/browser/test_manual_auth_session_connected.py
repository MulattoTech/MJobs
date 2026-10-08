"""Connected UI acceptance for deliberate MOCK reauthentication and rerun."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
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
                     "Build apps/web before connected browser acceptance")
class ManualAuthSessionConnectedBrowserTests(unittest.TestCase):
    def test_auth_sign_in_reuses_mock_profile_and_rerun_needs_separate_click(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-manual-auth-connected-") as directory:
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
                attempt_count = store.count("application_attempts")
                expected_mock_profile = (root / application_id / "browser-profile").resolve()
                self.assertTrue(expected_mock_profile.is_dir())

                app = create_app(store, fixture, root,
                                 "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
                thread = threading.Thread(target=server.run,
                                          kwargs={"sockets": [sock]}, daemon=True)
                ui_requests = []
                external_requests = []
                cookie_responses = []

                def record_ui_request(request):
                    parsed = urlsplit(request.url)
                    if parsed.netloc not in {urlsplit(origin).netloc,
                                             urlsplit(fixture).netloc}:
                        external_requests.append(parsed.netloc)
                    if parsed.path.endswith(("/manual-auth", "/mock-run")):
                        # Retain only route, method, and the rerun's harmless control flag.
                        body = request.post_data_json if parsed.path.endswith("/mock-run") else None
                        ui_requests.append((request.method, parsed.path, body))

                def record_response(response):
                    # Record only the path; never retain a cookie value.
                    parsed = urlsplit(response.url)
                    if "set-cookie" in response.headers:
                        cookie_responses.append(parsed.path)

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

                            def local_only(route):
                                host = urlsplit(route.request.url).netloc
                                if host not in {urlsplit(origin).netloc,
                                                urlsplit(fixture).netloc}:
                                    route.abort()
                                else:
                                    route.continue_()

                            context.route("**/*", local_only)
                            page = context.new_page()
                            page.set_default_timeout(8000)
                            page.on("request", record_ui_request)
                            page.on("response", record_response)
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
                            expect(auth_panel).to_contain_text("MOCK fixture only")
                            expect(auth_panel).to_contain_text("does not automatically rerun")
                            expect(auth_panel).to_contain_text("does not collect or display credentials")
                            self.assertEqual(auth_panel.locator(
                                'input[type="password"], input[name*="cookie" i]').count(), 0)
                            self.assertNotIn("mock_session", auth_panel.inner_text())
                            self.assertNotIn("fixture-authenticated", auth_panel.inner_text())
                            self.assertFalse(cookie_responses)

                            # No implicit retry occurs while the review screen is opened.
                            self.assertEqual(ui_requests, [])
                            attempts_before_sign_in = store.count("application_attempts")
                            with page.expect_response(lambda response:
                                    response.url.endswith("/manual-auth")) as sign_in_response:
                                auth_panel.get_by_role(
                                    "button", name="Open MOCK sign-in", exact=True).click()
                            self.assertEqual(sign_in_response.value.status, 200)
                            open_window.assert_called_once()
                            self.assertEqual(open_window.call_args.kwargs["application_id"], application_id)
                            self.assertEqual(open_window.call_args.kwargs["mock_origin"], fixture)
                            self.assertEqual(Path(open_window.call_args.kwargs["output_dir"]).resolve(), root.resolve())
                            self.assertEqual(expected_mock_profile,
                                             (root / open_window.call_args.kwargs["application_id"]
                                              / "browser-profile").resolve())
                            self.assertEqual(ui_requests, [(
                                "POST", f"/profiles/fixture/applications/{application_id}/manual-auth", None)])
                            self.assertEqual(store.count("application_attempts"), attempts_before_sign_in)
                            self.assertEqual(store.application(application_id)["state"], "AUTH_REQUIRED")
                            self.assertFalse(external_requests)
                            self.assertFalse(cookie_responses)
                            self.assertEqual(httpx.get(f"{fixture}/_test/request-counts").json()["submit"], 0)

                            # Rerun happens only after its separate click and stays non-submitting.
                            rerun_button = auth_panel.get_by_role(
                                "button", name="Rerun MOCK application", exact=True)
                            expect(rerun_button).to_be_enabled()
                            with page.expect_response(lambda response:
                                    response.url.endswith("/mock-run") and response.status == 202):
                                rerun_button.click()
                            self.assertEqual(ui_requests[-1], (
                                "POST", f"/profiles/fixture/applications/{application_id}/mock-run",
                                {"submit": False}))
                            self.assertEqual(store.count("application_attempts"), attempt_count)
                            self.assertEqual(store.application(application_id)["state"], "AUTH_REQUIRED")
                            self.assertFalse(external_requests)
                            self.assertFalse(cookie_responses)
                            self.assertEqual(httpx.get(f"{fixture}/_test/request-counts").json()["submit"], 0)
                            context.close()
                            browser.close()
                    finally:
                        server.should_exit = True
                        thread.join(timeout=10)
                        sock.close()
                        store.engine.dispose()
                        self.assertFalse(thread.is_alive())
