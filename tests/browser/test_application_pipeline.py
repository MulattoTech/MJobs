"""A manual application pipeline is visible, honest, and survives reload."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationPipelineBrowserTests(unittest.TestCase):
    def test_user_reported_stage_and_note_survive_reload_with_documents_linked(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-application-pipeline-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            operational_before = store.application(application_id)
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated application pipeline API did not start")
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)
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
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()

                    board = page.get_by_role("region", name="Application pipeline")
                    expect(board).to_be_visible()
                    for label in ["Not tracked", "Saved", "Ready", "Applied — reported by you",
                                  "Recruiter Contact", "Interview", "Technical Interview",
                                  "Final Interview", "Offer", "Rejected", "Withdrawn"]:
                        expect(board.get_by_role("region", name=label, exact=True)).to_be_visible()
                    not_tracked = board.get_by_role("region", name="Not tracked")
                    card = not_tracked.get_by_role("button", name=job["title"], exact=False)
                    expect(card).to_be_visible()
                    card.click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    expect(tracking).to_contain_text("Not tracked")
                    page.get_by_label("Manual application status", exact=True).select_option("APPLIED")
                    tracking.get_by_role("button", name="Update status", exact=True).click()
                    expect(tracking).to_contain_text("Applied — reported by you")
                    expect(board.get_by_role("region", name="Applied").get_by_role(
                        "button", name=job["title"], exact=False)).to_be_visible()

                    note_text = "Synthetic note: emailed recruiter; awaiting reply."
                    page.get_by_label("Application note", exact=True).fill(note_text)
                    tracking.get_by_role("button", name="Add application note", exact=True).click()
                    history = tracking.get_by_role("list", name="Application history")
                    expect(history).to_contain_text("Applied — reported by you")
                    expect(history).to_contain_text(note_text)
                    expect(page.get_by_role("link", name="Download PDF", exact=True).first).to_be_visible()

                    after = store.application(application_id)
                    self.assertEqual(after["state"], operational_before["state"])
                    self.assertEqual(after["confirmation"], operational_before["confirmation"])
                    self.assertIsNone(after["confirmation"])
                    self.assertFalse(external)

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    board = page.get_by_role("region", name="Application pipeline")
                    applied_card = board.get_by_role("region", name="Applied").get_by_role(
                        "button", name=job["title"], exact=False)
                    expect(applied_card).to_be_visible()
                    applied_card.click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    expect(tracking).to_contain_text("Applied — reported by you")
                    expect(tracking.get_by_role("list", name="Application history")).to_contain_text(note_text)
                    expect(page.get_by_role("link", name="Download PDF", exact=True).first).to_be_visible()
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
