"""Browser proof for source-value selection and preserved-route preparation."""
import asyncio
import copy
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class SourceValueResolutionBrowserTests(unittest.TestCase):
    def test_quality_selection_and_preserved_route_are_real_ui_and_api_flows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {"id": "browser-source-company", "name": "Synthetic Source Employer", "domain": ""})

            rich = upsert_company_job_source(
                store, company_id="browser-source-company", provider="rss", tenant_identifier="browser-source-company",
                career_url="https://rich.example/careers", feed_url="https://rich.example/jobs.xml",
                collection_strategy="RSS", discovered_automatically=True,
            )
            poor = upsert_company_job_source(
                store, company_id="browser-source-company", provider="rss", tenant_identifier="browser-source-company",
                career_url="https://poor.example/careers", feed_url="https://poor.example/jobs.xml",
                collection_strategy="RSS", discovered_automatically=True,
            )
            rich_item = {
                "guid": "same-role", "title": "Python Engineer",
                "description": "Python engineering. Salary $90,000 - $110,000 per year.",
                "location": "Remote - United States", "link": "/jobs/one",
            }
            poor_item = {"guid": "same-role", "title": "Sparse role", "link": "/jobs/one"}
            job_id = identity("browser-source-company", "rss", "browser-source-company", "same-role")
            self.assertEqual(import_company_feed_jobs(
                store, company_id="browser-source-company", source_id=rich["id"], provider="rss",
                tenant="browser-source-company", endpoint=rich["endpoint"], strategy="RSS",
                items=[rich_item], profile_id="fixture", fetch_available=True,
            ), 1)
            self.assertEqual(import_company_feed_jobs(
                store, company_id="browser-source-company", source_id=poor["id"], provider="rss",
                tenant="browser-source-company", endpoint=poor["endpoint"], strategy="RSS",
                items=[poor_item], profile_id="fixture", fetch_available=True,
            ), 1)
            initial = store.job(job_id, "fixture")
            self.assertEqual(initial["title"], "Python Engineer")
            application_id = prepare(store, job_id, "fixture", root)
            old_route = initial["application_url"]

            changed = dict(rich_item, link="/jobs/two")
            self.assertEqual(import_company_feed_jobs(
                store, company_id="browser-source-company", source_id=rich["id"], provider="rss",
                tenant="browser-source-company", endpoint=rich["endpoint"], strategy="RSS",
                items=[changed], profile_id="fixture", fetch_available=True,
            ), 1)
            refreshed = store.job(job_id, "fixture")
            new_route = refreshed["application_url"]
            self.assertNotEqual(new_route, old_route)
            self.assertEqual(application_id, identity(job_id, "fixture"))
            self.assertTrue(refreshed["attributes"]["source_resolution"]["route_review_required"])

            app = create_app(store, "http://127.0.0.1:1", root, "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=2))

            def serve_isolated_api():
                # Keep this test's server on a selector loop: Windows' default
                # Proactor teardown can surface client disconnects as an
                # unhandled socket shutdown while the test is already done.
                with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                    runner.run(server.serve(sockets=[sock]))

            thread = threading.Thread(target=serve_isolated_api, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated browser API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1365, "height": 1000})
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.locator("#active-profile").select_option("fixture")
                    card = page.locator(".job-card").filter(has_text="Synthetic Source Employer").first
                    expect(card).to_be_visible()
                    card.click()
                    detail = page.locator(".job-detail")
                    expect(detail).to_contain_text("Python Engineer")
                    expect(detail).to_contain_text("Selected source evidence")
                    expect(detail).to_contain_text("Source authority not established")
                    expect(detail).to_contain_text("Source disagreements")
                    expect(detail).to_contain_text("Selected application URL: https://rich.example/jobs/two")
                    expect(detail.get_by_role("button", name="Prepare documents", exact=True)).to_be_disabled()
                    expect(detail).to_contain_text("Previously used route: https://rich.example/jobs/one")

                    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/applications")) as request_info:
                        detail.get_by_role("button", name="Prepare using preserved route", exact=True).click()
                    queued = request_info.value.json()
                    self.assertEqual(queued["route_mode"], "THIS_ROUTE")
                    self.assertEqual(queued["route_ids"], [refreshed["attributes"]["source_resolution"]["preserved_route_id"]])
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()

                with store.engine.connect() as connection:
                    operation = connection.execute(select(m.operations).where(
                        m.operations.c.kind == "PREPARE", m.operations.c.entity_id == "fixture").order_by(
                            m.operations.c.updated_at.desc())).mappings().first()
                self.assertIsNotNone(operation)
                self.assertEqual(operation["params"]["route_mode"], "THIS_ROUTE")
                self.assertEqual(operation["params"]["route_id"], refreshed["attributes"]["source_resolution"]["preserved_route_id"])

            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated test API must stop")


if __name__ == "__main__":
    unittest.main()
