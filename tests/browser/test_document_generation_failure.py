"""Renderer failures reach Activity and recover only through an explicit retry."""
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
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class DocumentGenerationFailureBrowserTests(unittest.TestCase):
    def test_partial_renderer_failure_is_visible_durable_and_explicitly_recoverable(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-document-failure-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "email": "other@example.invalid"})
            with fixture_server() as fixture_origin:
                discover(store, fixture_origin, "fixture")
                job_id = store.jobs("fixture")[0]["id"]
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(create_app(
                    store, fixture_origin, root, "redis://127.0.0.1:1/0",
                    operation_mode="FIXTURE"), log_level="critical"))
                thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
                external = []
                try:
                    thread.start()
                    deadline = time.monotonic() + 10
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            self.fail("Isolated document-failure API did not start")
                        time.sleep(.02)
                    with sync_playwright() as playwright, playwright.chromium.launch(headless=True) as browser:
                        page = browser.new_page()
                        page.set_default_timeout(8000)

                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        page.route("**/*", local_only)
                        queued = page.request.post(origin + "/applications", data={
                            "profile_id": "fixture", "job_id": job_id, "tailor_resume": True})
                        self.assertEqual(queued.status, 202, queued.text())
                        operation_id = queued.json()["operation_id"]
                        application_id = queued.json()["application_id"]

                        def broken_renderer(path, *args, **kwargs):
                            Path(path).write_bytes(b"partial synthetic PDF")
                            raise OSError("PRIVATE_RENDERER_DIAGNOSTIC")

                        environment = {"MJOBS_DATABASE_URL": database_url,
                                       "MJOBS_MOCK_ORIGIN": fixture_origin,
                                       "MJOBS_OUTPUT_DIR": str(root), "MJOBS_OPERATION_MODE": "FIXTURE"}
                        with patch.dict("os.environ", environment), patch(
                                "services.documents.generate.render_pdf", side_effect=broken_renderer):
                            result = execute_operation(operation_id)
                        self.assertEqual(result["status"], "DEAD_LETTERED")
                        self.assertEqual(store.count("applications"), 0)
                        self.assertEqual(store.count("documents"), 0)
                        self.assertEqual(store.count("application_attempts"), 0)
                        for pattern in ("*.pdf", "*.docx", "*.txt", "*.tmp"):
                            self.assertEqual(list(root.rglob(pattern)), [])
                        self.assertEqual(page.request.get(
                            origin + f"/profiles/fixture/applications/{application_id}").status, 404)

                        page.goto(origin, wait_until="domcontentloaded")
                        page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Activity", exact=True).click()
                        panel = page.get_by_role("region", name="Dead-letter queue")
                        expect(panel.get_by_role("heading", name="Document generation failed")).to_be_visible()
                        expect(panel).to_contain_text("DOCUMENT_GENERATION_FAILED")
                        expect(panel).to_contain_text("Do not use or submit an incomplete file")
                        self.assertNotIn("PRIVATE_RENDERER_DIAGNOSTIC", page.locator("body").inner_text())
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Activity", exact=True).click()
                        expect(panel).to_contain_text("DOCUMENT_GENERATION_FAILED")
                        page.get_by_label("Active profile", exact=True).select_option("other")
                        expect(panel).to_contain_text("No tasks are dead-lettered")
                        page.get_by_label("Active profile", exact=True).select_option("fixture")
                        expect(panel).to_contain_text("DOCUMENT_GENERATION_FAILED")
                        with page.expect_response(lambda response: response.url.endswith(
                                f"/operations/{operation_id}/retry") and response.request.method == "POST") as retry:
                            panel.get_by_role("button", name="Retry task").click()
                        self.assertEqual(retry.value.status, 202)
                        with patch.dict("os.environ", environment):
                            self.assertEqual(execute_operation(operation_id)["status"], "COMPLETED")
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Activity", exact=True).click()
                        expect(panel).to_contain_text("No tasks are dead-lettered")
                        self.assertEqual(store.count("documents"), 2)
                        self.assertEqual(store.count("application_attempts"), 0)
                        self.assertEqual(external, [])
                finally:
                    server.should_exit = True
                    thread.join(10)
                    sock.close()
                    store.engine.dispose()
                    self.assertFalse(thread.is_alive(), "Isolated document-failure API leaked")
            reopened = Store(database_url)
            try:
                self.assertEqual(reopened.count("documents"), 2)
                self.assertEqual(reopened.application(application_id)["state"], "PREPARED")
            finally:
                reopened.engine.dispose()
