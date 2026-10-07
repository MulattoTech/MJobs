"""Connected local browser proof for dead-letter review and operator actions."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class DeadLetterQueueBrowserTests(unittest.TestCase):
    def test_retry_and_discard_are_visible_and_survive_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-dead-letter-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            retry_id = enqueue_operation(store, "DISCOVER", "fixture", {"request_id": "retry"})
            discard_id = enqueue_operation(store, "DISCOVER", "fixture", {"request_id": "discard"})
            timestamp = "2026-10-07T00:00:00+00:00"
            with store.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id.in_([retry_id, discard_id])).values(
                    state="DEAD_LETTERED", error="RETRY_LIMIT", attempt_count=3,
                    last_attempt_at=timestamp, dead_lettered_at=timestamp))

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated dead-letter API did not start")
                    time.sleep(0.02)
                external = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    panel = page.get_by_role("region", name="Dead-letter queue")
                    expect(panel).to_contain_text("2 tasks need operator review")
                    expect(panel).to_contain_text("RETRY_LIMIT")
                    expect(panel).to_contain_text("Last attempt:")

                    retry_card = panel.locator(".operation").filter(has_text=retry_id[:16])
                    retry_card.get_by_role("button", name="Retry task").click()
                    expect(panel).to_contain_text("1 task needs operator review")
                    with store.engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.operations.c.state).where(
                            m.operations.c.id == retry_id)).scalar_one(), "QUEUED")

                    page.once("dialog", lambda dialog: dialog.accept())
                    discard_card = panel.locator(".operation").filter(has_text=discard_id[:16])
                    discard_card.get_by_role("button", name="Discard").click()
                    expect(panel).to_contain_text("No tasks are dead-lettered.")
                    with store.engine.connect() as connection:
                        discarded = connection.execute(select(m.operations).where(
                            m.operations.c.id == discard_id)).mappings().one()
                        self.assertEqual(discarded["state"], "DISCARDED")
                        self.assertEqual(discarded["error"], "RETRY_LIMIT")

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Activity", exact=True).click()
                    panel = page.get_by_role("region", name="Dead-letter queue")
                    expect(panel).to_contain_text("0 tasks need operator review")
                    self.assertFalse(external)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
