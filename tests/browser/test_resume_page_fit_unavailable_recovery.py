"""Browser recovery when deterministic PDF measurement is unavailable."""
from pathlib import Path
import copy
import hashlib
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

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


class ResumePageFitUnavailableRecoveryBrowserTests(unittest.TestCase):
    def test_unavailable_measurement_keeps_source_unchanged_and_allows_leaving_page_fit(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-page-fit-unavailable-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'page-fit-unavailable.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            source_profile = store.profile("fixture")

            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            before_rows = application_documents(store, application_id, root)
            source_resume = next(row for row in before_rows if row["kind"] == "resume")
            source_bytes = {
                kind: (root / application_id / relative).read_bytes()
                for kind, relative in source_resume["files"].items()
            }
            source_hashes = {
                kind: hashlib.sha256(value).hexdigest()
                for kind, value in source_bytes.items()
            }

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            unavailable_result = {
                "page_target": 1,
                "page_count": None,
                "attempts": 1,
                "measurement_available": False,
                "target_met": False,
                "reason": "PAGE_MEASUREMENT_UNAVAILABLE",
                "omitted_facts": [],
            }
            generation_calls = []
            preview_runner_calls = []

            def unavailable_preview_runner(
                runner_store, profile_id, job_id, output_dir, target_pdf_pages,
            ):
                preview_runner_calls.append((profile_id, job_id, target_pdf_pages))
                return {"page_fit": dict(unavailable_result)}

            def unexpected_generation(*args, **kwargs):
                generation_calls.append((args, kwargs))
                raise AssertionError("unreviewed/unmeasurable page-fit preview was generated")

            # create_app captures these default runners when the router is built.
            # Restore the module immediately afterward; the isolated app retains the spies.
            preview_patch = patch(
                "apps.api.resume_page_fit._default_preview_runner",
                side_effect=unavailable_preview_runner,
            )
            generate_patch = patch(
                "apps.api.resume_page_fit._default_generate_runner",
                side_effect=unexpected_generation,
            )
            preview_patch.start()
            generate_patch.start()
            try:
                app = create_app(
                    store, "http://127.0.0.1:1", root,
                    "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
                )
            finally:
                generate_patch.stop()
                preview_patch.stop()

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
                    generation_requests = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            outside.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: self.fail(str(error)))

                    def observe_generate(route):
                        if (route.request.method == "POST" and
                                route.request.url.endswith("/generate")):
                            generation_requests.append(route.request.url)
                        route.continue_()

                    page.route("**/resume-page-fit-previews/**", observe_generate)
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
                    expect(preview.get_by_text("PDF page count unavailable", exact=True)).to_be_visible()
                    expect(preview.get_by_text(
                        "The page target could not be measured; no page count is estimated.",
                        exact=True,
                    )).to_be_visible()
                    expect(preview.get_by_text("Render attempts: 1. Attempts are bounded.", exact=True)).to_be_visible()
                    expect(preview).to_contain_text("No approved facts were omitted from this preview.")
                    expect(preview).not_to_contain_text("Measured PDF pages:")
                    expect(preview).not_to_contain_text("I reviewed the measured PDF page-fit preview")
                    expect(page.get_by_role(
                        "button", name="Generate reviewed PDF", exact=True,
                    )).to_be_disabled()

                    # Leaving an unavailable page-fit preview is a manual recovery path;
                    # it must not publish a derivative or alter the source profile.
                    page.get_by_role("button", name="Profile", exact=True).click()
                    expect(page.get_by_role("button", name="Applications", exact=False).first).to_be_visible()
                    self.assertEqual(preview_runner_calls, [("fixture", job["id"], 1)])
                    self.assertEqual(generation_calls, [])
                    self.assertEqual(generation_requests, [])
                    self.assertFalse(outside)

                    after_rows = application_documents(store, application_id, root)
                    self.assertEqual(after_rows, before_rows)
                    retained = next(row for row in after_rows if row["id"] == source_resume["id"])
                    self.assertEqual(retained["sha256"], source_hashes)
                    for kind, relative in retained["files"].items():
                        self.assertEqual((root / application_id / relative).read_bytes(), source_bytes[kind])
                    self.assertEqual(store.profile("fixture"), source_profile)
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
