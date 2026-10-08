"""Connected synthetic browser flow for reviewed company context."""
import copy
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
from packages.persistence.store import Store
from services.company_discovery.company_context import list_company_contexts
from services.company_discovery.domains import record_company_domain
from services.company_discovery.sources import upsert_company_job_source
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class CompanyContextBrowserTests(unittest.TestCase):
    def test_context_review_persists_through_local_api_and_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-company-context-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            with store.engine.begin() as connection:
                from packages.persistence import models as m
                from packages.persistence.store import upsert
                upsert(connection, m.companies, {
                    "id": "acme", "name": "Acme", "domain": "acme.example",
                })
            source = upsert_company_job_source(
                store, company_id="acme", provider="rss", tenant_identifier="acme",
                career_url="https://careers.acme.example/jobs",
                feed_url="https://careers.acme.example/jobs.xml",
                collection_strategy="RSS", confidence=0.90,
            )
            record_company_domain(store, "acme", "acme.example",
                                  "VERIFIED_EXISTING_DOMAIN", ["synthetic verified record"])
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            external = []
            errors = []
            browser = None
            context = None
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated company-context API did not start")
                    time.sleep(0.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    try:
                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(origin, wait_until="domcontentloaded")
                        page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Sources", exact=True).click()
                        card = page.get_by_role("article", name="Acme source")
                        expect(card).to_be_visible()
                        expect(card).to_contain_text("No company context saved.")
                        page.get_by_role("textbox", name="Company context for Acme", exact=True).fill(
                            "Acme provides regional infrastructure support.")
                        page.get_by_label("Company context source URL for Acme", exact=True).fill(
                            "https://careers.acme.example/about/company")
                        with page.expect_response(lambda response:
                            response.url.endswith(f"/company-sources/{source['id']}/contexts")
                            and response.request.method == "POST") as save_response:
                            page.get_by_role("button", name="Save user-provided context", exact=True).click()
                        self.assertEqual(save_response.value.status, 201, save_response.value.text())
                        expect(card).to_contain_text(
                            "USER_PROVIDED · CONTENT UNVERIFIED · SOURCE IDENTITY VERIFIED")
                        checkbox = card.get_by_label(
                            "I reviewed this USER_PROVIDED context and approve its use in job-specific letters.")
                        checkbox.check()
                        with page.expect_response(lambda response:
                            response.url.endswith("/review") and response.request.method == "POST") as review_response:
                            card.get_by_role("button", name="Approve context", exact=True).click()
                        self.assertEqual(review_response.value.status, 200, review_response.value.text())
                        expect(card).to_contain_text("Reviewed and approved for job-specific letters.")
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name="Sources", exact=True).click()
                        card = page.get_by_role("article", name="Acme source")
                        expect(card).to_contain_text("Reviewed and approved for job-specific letters.")
                    finally:
                        context.close()
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertEqual(external, [])
            self.assertEqual(errors, [])
            persisted = list_company_contexts(store, source["id"])
            self.assertEqual(len(persisted), 1)
            self.assertTrue(persisted[0]["user_approved"])
            store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
