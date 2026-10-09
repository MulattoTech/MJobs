"""Connected success path for reviewed resume PDF page-fit generation."""
from pathlib import Path
import copy
import hashlib
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
from services.documents.history import application_documents
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ResumePageFitMeasuredSuccessBrowserTests(unittest.TestCase):
    def test_reviewed_measurement_persists_fitted_revision_and_preserves_source(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-page-fit-success-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'page-fit-success.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            source_application = prepare(store, job["id"], "fixture", root)
            source_rows = application_documents(store, source_application, root)
            source_resume = next(row for row in source_rows if row["kind"] == "resume")
            source_path = root / source_application / source_resume["files"]["pdf"]
            source_bytes = source_path.read_bytes()
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            applications_before = store.count("applications")
            documents_before = store.count("documents")

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(
                store, "http://127.0.0.1:1", root,
                "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
            )
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Resume page-fit API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    outside = []
                    forbidden_writes = []
                    fit_requests = []

                    def local_only(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            outside.append(request.url)
                            route.abort()
                            return
                        if request.method == "POST" and any(
                            token in parsed.path for token in ("/applications", "/collect", "/submit")
                        ):
                            forbidden_writes.append((request.method, parsed.path))
                            route.abort()
                            return
                        if request.method == "POST" and "/resume-page-fit-previews" in parsed.path:
                            fit_requests.append((request.method, parsed.path))
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: self.fail(str(error)))
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload()
                    page.get_by_role("button", name="Applications", exact=False).first.click()
                    page.locator(".job-card").filter(has_text=job["title"]).first.click()

                    fit = page.get_by_role("region", name="Resume PDF page fit", exact=True)
                    expect(fit).to_be_visible()
                    page.get_by_label("Target PDF pages", exact=True).fill("1")
                    page.get_by_role("button", name="Preview page fit", exact=True).click()
                    preview = page.get_by_role(
                        "region", name="Measured PDF page-fit preview", exact=True,
                    )
                    expect(preview).to_contain_text("Measured PDF pages: 1")
                    expect(preview).to_contain_text(
                        "The measured PDF meets the requested page target."
                    )
                    self.assertEqual(store.count("applications"), applications_before)
                    self.assertEqual(store.count("documents"), documents_before)
                    generate = page.get_by_role(
                        "button", name="Generate reviewed PDF", exact=True,
                    )
                    expect(generate).to_be_disabled()
                    review = page.get_by_label(
                        "I reviewed the measured PDF page-fit preview and omissions", exact=True,
                    )
                    review.check()
                    expect(generate).to_be_enabled()
                    generate.click()

                    generated = page.get_by_role("status", name="Generated page-fit PDF", exact=True)
                    expect(generated).to_contain_text("Prepared resume version")
                    self.assertEqual(store.count("applications"), applications_before)
                    self.assertEqual(store.count("documents"), documents_before + 2)
                    self.assertEqual(len(fit_requests), 2)
                    self.assertEqual(forbidden_writes, [])
                    self.assertEqual(outside, [])

                    rows = application_documents(store, source_application, root)
                    retained = next(row for row in rows if row["id"] == source_resume["id"])
                    self.assertEqual(retained["sha256"]["pdf"], source_hash)
                    self.assertEqual(source_path.read_bytes(), source_bytes)

                    generated_rows = application_documents(
                        store, source_application, root,
                    )
                    fitted = next(
                        row for row in generated_rows
                        if row["kind"] == "resume" and row["id"] != source_resume["id"]
                    )
                    page_fit = fitted["generation_context"]["page_fit"]
                    self.assertEqual(page_fit["measurement_status"], "MEASURED")
                    self.assertEqual(page_fit["target_page_count"], 1)
                    self.assertLessEqual(page_fit["actual_page_count"], 1)
                    self.assertFalse(page_fit["unmet_target"])
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
