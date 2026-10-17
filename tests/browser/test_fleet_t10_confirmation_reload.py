"""Reload saved uncertain receipt evidence through the actual dashboard/API."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import httpx
from playwright.sync_api import sync_playwright, expect
from sqlalchemy import update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build web before browser acceptance")
class FleetT10ConfirmationReloadTests(unittest.TestCase):
    def test_timeout_then_missing_receipt_survive_dashboard_reload_without_submission(self):
        with tempfile.TemporaryDirectory(prefix="fleet-t10-browser-") as directory, fixture_server() as fixture:
            root = Path(directory)
            url = f"sqlite:///{(root / 'fixture.db').as_posix()}"
            store = Store(url)
            store.migrate(); store.bind_workspace_mode("FIXTURE"); store.save_profile(dict(PROFILE))
            discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            app_id = prepare(store, job["id"], "fixture", root)
            store.transition(app_id, ["PREPARED"], "SUBMISSION_UNCONFIRMED")
            path = f"/profiles/fixture/applications/{app_id}/confirmation-check"
            with TestClient(create_app(store, fixture, root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
                with patch("services.applications.mock_adapter._receipt", side_effect=TimeoutError("synthetic deadline")):
                    self.assertEqual(client.post(path, json={"request_id": "before-restart"}).status_code, 200)
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
                    if not thread.is_alive() or time.monotonic() >= deadline:
                        self.fail("Fixture API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        context = browser.new_context()
                        external = []
                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url); route.abort()
                            else:
                                route.continue_()
                        context.route("**/*", local_only)
                        page = context.new_page(); page.set_default_timeout(8000)
                        posts = []
                        page.on("request", lambda request: posts.append(request.url)
                                if request.url.endswith("/confirmation-check") and request.method == "POST" else None)
                        page.goto(origin, wait_until="domcontentloaded")
                        page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                        def open_application():
                            page.reload(wait_until="domcontentloaded")
                            page.get_by_role("button", name="Applications", exact=False).click()
                            page.get_by_role("region", name="Application pipeline").get_by_role(
                                "button", name=job["title"], exact=False).click()
                            return page.get_by_role("region", name="Verify uncertain mock submission", exact=True)
                        panel = open_application()
                        expect(panel).to_contain_text("unavailable")
                        expect(panel).to_contain_text("submission remains uncertain")
                        self.assertEqual(posts, [])
                        panel.get_by_role("button", name="Check receipt only").click()
                        expect(panel).to_contain_text("No verified receipt")
                        self.assertEqual(len(posts), 1)
                        panel = open_application()
                        expect(panel).to_contain_text("No verified receipt")
                        self.assertEqual(len(posts), 1)
                        # A stale submitting screen must refresh its application
                        # state after an explicit check resolves to unconfirmed.
                        with reopened.engine.begin() as connection:
                            connection.execute(update(m.applications).where(
                                m.applications.c.id == app_id).values(state="SUBMITTING"))
                        panel = open_application()
                        panel.get_by_role("button", name="Check receipt only").click()
                        expect(panel).to_contain_text("No verified receipt")
                        self.assertEqual(len(posts), 2)
                        self.assertEqual(reopened.application(app_id)["state"], "SUBMISSION_UNCONFIRMED")
                        self.assertEqual(httpx.get(fixture + "/_test/request-counts", trust_env=False).json(), {"review": 0, "submit": 0})
                        self.assertFalse(external)
                    finally:
                        browser.close()
            finally:
                server.should_exit = True; thread.join(timeout=10); sock.close()
                reopened.engine.dispose()
                self.assertFalse(thread.is_alive())
