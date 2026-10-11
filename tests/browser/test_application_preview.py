"""Browser acceptance for a durable, read-only application review screen."""
from pathlib import Path
import copy
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import delete

from apps.api.main import ApplicationAnswerItem, create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.answers import export_answers
from services.documents.generate import prepare
from services.applications.preflight import persist_preflight
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationPreviewBrowserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-application-preview-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'preview.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(copy.deepcopy(PROFILE))
        with fixture_server() as fixture:
            discover(self.store, fixture, "fixture")
        self.job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
        self.application_id = prepare(self.store, self.job["id"], "fixture", self.root)
        export_answers(self.store, self.application_id, self.root, [ApplicationAnswerItem(**{
            "question": "Describe a fictional project.",
            "text": "I built a small synthetic Python project.",
            "reviewed": True,
        })])

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _open(self):
        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         operation_mode="FIXTURE")
        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                self.fail("Isolated application preview API did not start")
            time.sleep(0.02)
        return origin, sock, server, thread

    def test_selected_application_preview_survives_reload_without_starting_automation(self):
        stored_application = self.store.application(self.application_id)
        persist_preflight(self.store, self.application_id, {
            "result": "PASS", "reason": None, "observed_at": "2026-10-08T10:30:00Z",
            "profile_version": stored_application["profile_version"],
            "document_version": stored_application["document_version"],
            "route_id": stored_application["route_id"], "provider": "mock-ats",
            "tenant": "example", "requisition": "B", "metadata_fingerprint": "b" * 64,
            "mappings": [], "missing": [],
        })
        before = self.store.application(self.application_id)
        before_documents = self.store.count("documents")
        before_attempts = self.store.count("application_attempts")
        origin, sock, server, thread = self._open()
        methods, outside, errors = [], [], []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

                def local_only(route):
                    methods.append((route.request.method, route.request.url))
                    if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                        outside.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", local_only)
                page = context.new_page()
                page.set_default_timeout(8000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin, wait_until="domcontentloaded")
                page.get_by_role("navigation", name="Main navigation").get_by_role(
                    "button", name="Applications", exact=False).click()
                page.get_by_role("region", name="Not tracked").get_by_role(
                    "button", name=self.job["title"], exact=False).click()

                review = page.get_by_role("region", name="Application review", exact=True)
                expect(review).to_contain_text(self.job["title"])
                expect(review).to_contain_text("Example Systems Inc.")
                expect(review).to_contain_text("Route")
                expect(review).to_contain_text("Resume")
                expect(review).to_contain_text("Cover letter")
                expect(review).to_contain_text("I built a small synthetic Python project.")
                expect(review).to_contain_text("Outstanding issues")
                expect(review).to_contain_text("UNKNOWN")
                expect(review).to_contain_text("Automation confidence")
                expect(page.get_by_role("region", name="ATS question preflight")).to_contain_text("PASS")
                issues = review.get_by_role("list", name="Outstanding application issues")
                expect(issues).not_to_contain_text("Required employer questions")
                expect(issues).not_to_contain_text("supported preflight evidence")

                page.reload(wait_until="domcontentloaded")
                review = page.get_by_role("region", name="Application review", exact=True)
                expect(review).to_contain_text(self.job["title"])
                expect(review).to_contain_text("I built a small synthetic Python project.")
                expect(review).to_contain_text("Automation confidence")
                expect(page.get_by_role("region", name="ATS question preflight")).to_contain_text("PASS")
                expect(review.get_by_role("list", name="Outstanding application issues")).not_to_contain_text("supported preflight evidence")
                expect(page.get_by_role("button", name="Run dry-run")).to_have_count(1)
                self.assertTrue(all(method == "GET" for method, _ in methods), methods)
                self.assertEqual(outside, [])
                self.assertEqual(errors, [])
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
        self.assertTrue(thread.is_alive() is False)
        self.assertEqual(self.store.application(self.application_id), before)
        self.assertEqual(self.store.count("documents"), before_documents)
        self.assertEqual(self.store.count("application_attempts"), before_attempts)

    def test_missing_cover_letter_stays_unknown_and_is_shown_as_a_blocker(self):
        with self.store.engine.begin() as connection:
            connection.execute(delete(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.kind == "cover-letter"))
            connection.execute(delete(m.application_route_choices).where(
                m.application_route_choices.c.application_id == self.application_id))
        origin, sock, server, thread = self._open()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")
                page = context.new_page()
                page.set_default_timeout(8000)
                page.goto(origin, wait_until="domcontentloaded")
                page.get_by_role("navigation", name="Main navigation").get_by_role(
                    "button", name="Applications", exact=False).click()
                page.get_by_role("region", name="Not tracked").get_by_role(
                    "button", name=self.job["title"], exact=False).click()
                review = page.get_by_role("region", name="Application review", exact=True)
                expect(review).to_contain_text("UNKNOWN — no validated cover-letter preview is available.")
                expect(review).to_contain_text("UNKNOWN — no saved route choice is available.")
                expect(review).to_contain_text("Cover letter is missing")
                expect(review).to_contain_text("Selected application route: UNKNOWN.")
                expect(review).to_contain_text("UNKNOWN — no calibrated application-confidence score is saved.")
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
