"""Mounted UI rejects a substituted poll result and restores real durable metadata."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.browser.test_lever_form_review import FORM_FIXTURE, LEVER_ROUTE, PRIVATE_MARKERS
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build web first")
class FleetT14ReviewBindingTests(unittest.TestCase):
    def test_wrong_poll_identity_is_hidden_and_reload_recovers_saved_review(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-t14-binding-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'review.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
            application_id = prepare(store, job["id"], "fixture", root)
            application = store.application(application_id)
            with store.engine.begin() as connection:
                connection.execute(update(m.routes).where(m.routes.c.id == application["route_id"]).values(
                    provider="lever", tenant="example", requisition="12345678-abcd-1234-abcd-1234567890ab",
                    url=LEVER_ROUTE))
                connection.execute(update(m.applications).where(m.applications.c.id == application_id).values(target="REAL"))
            before = store.application(application_id)
            before_attempts = store.count("application_attempts")
            before_documents = store.count("documents")
            app = create_app(store, "http://127.0.0.1:1", root, operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            review_path = f"/profiles/fixture/applications/{application_id}/form-reviews"
            writes, external, errors, substitutions = [], [], [], []

            class FixtureResponse:
                url = LEVER_ROUTE
                headers = {"content-type": "text/html; charset=utf-8"}
                content = FORM_FIXTURE

            class FixturePublicClient:
                def __init__(self, website, check=None, total_timeout=None):
                    self.check = check

                def allowed(self, url):
                    return url == LEVER_ROUTE

                def get(self, url):
                    if url != LEVER_ROUTE:
                        raise AssertionError("Unexpected synthetic route")
                    if self.check:
                        self.check()
                    return FixtureResponse()

                def close(self):
                    pass

            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Synthetic review API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        context = browser.new_context()
                        context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

                        def local_only(route):
                            request = route.request
                            path = urlsplit(request.url).path
                            if urlsplit(request.url).netloc != urlsplit(origin).netloc:
                                external.append(request.url)
                                route.abort()
                                return
                            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                                writes.append((request.method, path))
                            if request.method == "GET" and path.startswith(review_path + "/") and not path.endswith("/latest"):
                                substitutions.append(path)
                                route.fulfill(json={"request_id": "another-request", "state": "COMPLETED",
                                    "error_code": None, "analysis": {"status": "ANALYZED", "fields": [
                                        {"name": "SUBSTITUTED_FIELD", "type": "text", "required": True,
                                         "review_reason": None, "value": "PRIVATE_SUBSTITUTED_VALUE"}]}})
                                return
                            route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.set_default_timeout(10000)
                        page.on("pageerror", lambda error: errors.append(str(error)))

                        def open_application():
                            page.get_by_role("button", name="Applications", exact=False).click()
                            page.get_by_role("region", name="Application pipeline").get_by_role(
                                "button", name=job["title"], exact=False).click()
                            return page.get_by_role("region", name="Application form review")

                        page.goto(origin, wait_until="domcontentloaded")
                        review = open_application()
                        inspect = review.get_by_role("button", name="Inspect form fields (read-only)", exact=True)
                        expect(inspect).to_be_enabled()
                        self.assertEqual(writes, [])
                        with page.expect_response(lambda response:
                                response.request.method == "POST" and urlsplit(response.url).path == review_path) as queued:
                            inspect.click()
                        self.assertEqual(queued.value.status, 202)
                        operation_id = queued.value.json()["operation_id"]
                        expect(review.get_by_role("alert")).to_be_visible()
                        self.assertNotIn("SUBSTITUTED_FIELD", page.content())
                        self.assertNotIn("PRIVATE_SUBSTITUTED_VALUE", page.content())
                        self.assertTrue(substitutions)

                        with patch.dict(os.environ, {"MJOBS_OPERATION_MODE": "FIXTURE", "MJOBS_DATABASE_URL": database_url,
                                "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0", "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
                                "MJOBS_OUTPUT_DIR": str(root / "documents")}), patch("workers.tasks.PublicCompanyClient", FixturePublicClient):
                            result = execute_operation(operation_id)
                        self.assertEqual(result["status"], "COMPLETED", result)
                        page.reload(wait_until="domcontentloaded")
                        review = open_application()
                        expect(review.locator('[aria-label="Inspected form fields"]')).to_contain_text("first_name")
                        for marker in PRIVATE_MARKERS:
                            self.assertNotIn(marker.decode(), page.content())
                        self.assertEqual(writes, [("POST", review_path)])
                        self.assertEqual(external, [])
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
                self.assertEqual(store.application(application_id), before)
                self.assertEqual(store.count("application_attempts"), before_attempts)
                self.assertEqual(store.count("documents"), before_documents)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive())
