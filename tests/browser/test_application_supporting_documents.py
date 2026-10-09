"""Connected local mock application flow for optional supporting documents."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
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
class ApplicationSupportingDocumentsBrowserTests(unittest.TestCase):
    def test_select_reload_and_recover_job_scoped_supporting_document(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-supporting-documents-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'supporting-documents.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            with TestClient(app) as seed_client:
                uploaded = seed_client.post(
                    "/profiles/fixture/document-library",
                    content=b"Synthetic, unreviewed supporting evidence.",
                    headers={
                        "Content-Type": "text/plain",
                        "X-Filename": "supporting-evidence.txt",
                        "X-Document-Kind": "SUPPORTING_DOCUMENT",
                        "X-Document-Label": "Synthetic supporting evidence",
                    },
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                revision_id = uploaded.json()["revision_id"]

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run,
                                      kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Supporting-document application API did not start")
                    time.sleep(0.02)

                errors, external = [], []
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
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    main_nav = page.get_by_role("navigation", name="Main navigation")
                    main_nav.get_by_role("button").filter(has_text="Applications").click()
                    pipeline = page.get_by_role("region", name="Application pipeline")
                    card = pipeline.get_by_role("button", name=job["title"], exact=False)
                    expect(card).to_be_visible()
                    card.click()

                    panel = page.get_by_role(
                        "region", name="Supporting documents for this mock application",
                    )
                    expect(panel).to_be_visible()
                    checkbox = panel.get_by_role("checkbox", name="Synthetic supporting evidence")
                    checkbox.check()
                    panel.get_by_role(
                        "button", name="Save supporting document selection", exact=True,
                    ).click()
                    expect(panel.get_by_role("status")).to_contain_text("saved and confirmed")
                    self.assertEqual(context.request.get(
                        origin + f"/profiles/fixture/applications/{application_id}"
                        "/supporting-documents",
                    ).json()["selected_revision_ids"], [revision_id])

                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    main_nav = page.get_by_role("navigation", name="Main navigation")
                    main_nav.get_by_role("button").filter(has_text="Applications").click()
                    pipeline = page.get_by_role("region", name="Application pipeline")
                    pipeline.get_by_role("button", name=job["title"], exact=False).click()
                    panel = page.get_by_role(
                        "region", name="Supporting documents for this mock application",
                    )
                    expect(panel.get_by_role(
                        "checkbox", name="Synthetic supporting evidence",
                    )).to_be_checked()
                    self.assertEqual(store.application(application_id)["state"], "PREPARED")
                    self.assertFalse(context.request.get(origin + "/health").json()[
                        "external_submission_enabled"])
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Supporting-documents server must stop")


if __name__ == "__main__":
    unittest.main()
