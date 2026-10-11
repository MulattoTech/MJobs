"""Connected browser proof for safe, durable operation progress in Activity."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from services.operations.progress import record_operation_progress
from workers.dispatch import enqueue_operation


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class OperationProgressBrowserTests(unittest.TestCase):
    def test_saved_progress_is_safe_profile_scoped_and_survives_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-operation-progress-browser-") as directory:
            root = Path(directory)
            database = root / "browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                "email": "other@example.test"})

            private_params = "PRIVATE_OPERATION_PARAMS_SENTINEL"
            private_error = "PRIVATE_OPERATION_ERROR_SENTINEL"
            operation_id = enqueue_operation(store, "DISCOVER", "fixture", {
                "query": private_params,
                "request_id": "operation-progress-browser-fixture",
            })
            record_operation_progress(
                store,
                operation_id,
                current_step="FETCHING_POSTINGS",
                progress=47,
                state="RUNNING",
                retry_count=2,
                recovery_metadata={"action": "RETRY", "reason_code": "QUEUE_TIMEOUT"},
            )
            other_operation_id = enqueue_operation(store, "DISCOVER", "other", {
                "query": "OTHER_PROFILE_PRIVATE_SENTINEL",
            })
            record_operation_progress(
                store,
                other_operation_id,
                current_step="PERSISTING",
                progress=81,
                state="RUNNING",
            )
            # Emulate an internal worker error containing private detail. The public
            # operation progress contract must omit it, just as it omits params.
            with store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == operation_id).values(error=private_error))

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external_requests = []
            page_errors = []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated operation-progress API did not start")
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
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    progress_path = "/profiles/fixture/operations/progress"
                    response = page.request.get(origin + progress_path)
                    self.assertEqual(response.status, 200, response.text())
                    body = response.json()
                    self.assertEqual([item["operation_id"] for item in body["operations"]],
                                     [operation_id])
                    row = body["operations"][0]
                    self.assertEqual(row["progress"], 47)
                    self.assertEqual(row["current_step"], "FETCHING_POSTINGS")
                    self.assertEqual(row["retry_count"], 2)
                    self.assertEqual(row["recovery_summary"], "RETRY: QUEUE_TIMEOUT")
                    serialized_response = response.text()
                    self.assertNotIn(private_params, serialized_response)
                    self.assertNotIn(private_error, serialized_response)
                    self.assertNotIn("OTHER_PROFILE_PRIVATE_SENTINEL", serialized_response)

                    with page.expect_response(lambda item:
                            urlsplit(item.url).path == progress_path):
                        page.get_by_role("button", name="Activity", exact=True).click()
                    panel = page.get_by_role("region", name="Saved operation progress", exact=True)
                    expect(panel.get_by_role("heading", name="Background operations", exact=True)).to_be_visible()
                    card = panel.get_by_role("article", name="Discovery operation", exact=True)
                    expect(card).to_contain_text("In progress")
                    expect(card).to_contain_text("Fetching postings")
                    expect(card).to_contain_text("47%")
                    retry_count = card.locator("dl div").filter(has_text="Retry attempts")
                    expect(retry_count.locator("dd")).to_have_text("2")
                    expect(card).to_contain_text("Recovery details are available.")
                    visible_text = panel.inner_text()
                    self.assertNotIn(private_params, visible_text)
                    self.assertNotIn(private_error, visible_text)
                    self.assertNotIn("OTHER_PROFILE_PRIVATE_SENTINEL", visible_text)

                    # Closing the SQLAlchemy pool and reloading forces a fresh SQLite
                    # connection; the Activity tab must read the persisted row again.
                    store.engine.dispose()
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    with page.expect_response(lambda item:
                            urlsplit(item.url).path == progress_path):
                        page.get_by_role("button", name="Activity", exact=True).click()
                    reloaded_panel = page.get_by_role(
                        "region", name="Saved operation progress", exact=True)
                    reloaded_card = reloaded_panel.get_by_role(
                        "article", name="Discovery operation", exact=True)
                    expect(reloaded_card).to_contain_text("Fetching postings")
                    expect(reloaded_card).to_contain_text("47%")
                    expect(reloaded_card).to_contain_text("2")

                    other_path = "/profiles/other/operations/progress"
                    with page.expect_response(lambda item:
                            urlsplit(item.url).path == other_path):
                        page.get_by_label("Active profile", exact=True).select_option("other")
                    other_response = page.request.get(origin + other_path)
                    self.assertEqual(other_response.status, 200, other_response.text())
                    other_rows = other_response.json()["operations"]
                    self.assertEqual([item["operation_id"] for item in other_rows],
                                     [other_operation_id])
                    expect(page.get_by_role("region", name="Saved operation progress", exact=True)
                           ).to_contain_text("81%")
                    self.assertNotIn(operation_id, other_response.text())
                    self.assertEqual(external_requests, [])
                    self.assertEqual(page_errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Operation-progress API server did not stop")


if __name__ == "__main__":
    unittest.main()
