"""Connected review-to-local-task flow with collection and external writes disabled."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from services.source_candidates.catalogue import report_source_candidate


ROOT = Path(__file__).resolve().parents[2]
PROFILE = {
    "id": "fixture", "name": "Alex Fixture", "email": "alex@example.test",
    "answers": [], "facts": [],
}


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SourceCandidateLocalTaskBrowserTests(unittest.TestCase):
    def test_review_queues_local_task_with_readback_and_no_collection(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-source-local-task-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'source-candidate.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            candidate = report_source_candidate(
                store, profile_id="fixture", request_id="browser-local-task-candidate",
                source_url="https://jobs.unknown.invalid/role", application_url=None,
                discovery_origin="LinkedIn", observed_hops=[],
            )["candidate"]
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            outside_requests = []
            page_errors = []

            try:
                server_thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not server_thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated source-candidate API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            outside_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    card = page.get_by_role("article", name="Source candidate jobs.unknown.invalid")
                    expect(card).to_be_visible()
                    expect(card).to_contain_text("Collection is disabled")
                    card.get_by_label("Site type assessment for jobs.unknown.invalid").select_option("JOB_BOARD")
                    card.get_by_label("Review reason for jobs.unknown.invalid", exact=True).fill(
                        "Evaluate only documented and authorized feed options.")
                    card.get_by_label("API or feed evidence note for jobs.unknown.invalid").fill(
                        "No public feed has been verified.")
                    card.get_by_label("Access and terms note for jobs.unknown.invalid").fill(
                        "Terms and access requirements require review.")
                    card.get_by_label("Capabilities and limits note for jobs.unknown.invalid").fill(
                        "No collection is authorized by this queue action.")
                    card.get_by_label("Expected value and maintenance note for jobs.unknown.invalid").fill(
                        "Assess value and maintenance cost before adapter work.")
                    expect(card.get_by_label(
                        "Integration task issue number for jobs.unknown.invalid")).to_have_value("")
                    card.get_by_role("button", name="Queue local integration task", exact=True).click()
                    expect(card).to_contain_text("Review state: INTEGRATION QUEUED")
                    expect(card).to_contain_text(f"Local integration task keyed by source candidate {candidate['id']}")
                    expect(card).to_contain_text("Collection is disabled")

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    readback = page.get_by_role("article", name="Source candidate jobs.unknown.invalid")
                    expect(readback).to_contain_text("Review state: INTEGRATION QUEUED")
                    expect(readback).to_contain_text(candidate["id"])
                    expect(readback).to_contain_text("No integration issue is linked")
                    with store.engine.connect() as connection:
                        saved = connection.execute(select(m.source_candidates).where(
                            m.source_candidates.c.id == candidate["id"],
                            m.source_candidates.c.profile_id == "fixture",
                        )).mappings().one()
                        reviews = connection.execute(select(m.source_candidate_reviews).where(
                            m.source_candidate_reviews.c.candidate_id == candidate["id"],
                        )).mappings().all()
                    self.assertEqual(saved["state"], "INTEGRATION_QUEUED")
                    self.assertIsNone(saved["integration_issue"])
                    self.assertFalse(saved["collection_enabled"])
                    self.assertEqual(len(reviews), 1)
                    self.assertIsNone(reviews[0]["integration_issue"])
                    self.assertEqual(outside_requests, [])
                    self.assertEqual(page_errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                server_thread.join(timeout=5)
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
