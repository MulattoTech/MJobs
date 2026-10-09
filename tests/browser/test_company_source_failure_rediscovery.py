"""Connected UI readback for preserved company-source failure rediscovery."""
import copy
import os
from pathlib import Path
from urllib.parse import urlsplit
import tempfile
import threading
import time
import unittest

from fixtures.browser_ports import bound_browser_socket
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select, update
import uvicorn

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.monitoring import configure_monitoring, source_version
from services.company_discovery.sources import upsert_company_job_source
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class FailedCompanySourceRediscoveryBrowserTests(unittest.TestCase):
    def test_failed_source_and_queued_rediscovery_read_back_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-source-rediscovery-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'monitor.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            company_id = identity("public-company-origin", "https://employer.example")
            with store.engine.begin() as connection:
                upsert(connection, m.companies,
                       {"id": company_id, "name": "Example Employer", "domain": ""})
            source = upsert_company_job_source(
                store, company_id=company_id, provider="json_feed", tenant_identifier=company_id,
                career_url="https://employer.example/careers", feed_url="https://employer.example/jobs.json",
                collection_strategy="PUBLIC_JSON", confidence=0.45, enabled=True,
                discovered_automatically=True,
            )
            monitoring = configure_monitoring(store, source["id"], source_version(source), 0, 3600)
            with store.engine.begin() as connection:
                operation_id = enqueue_operation(None, "COMPANY_DISCOVER", company_id, {
                    "candidate": {"company_name": "Stored employer",
                                  "company_website": source["career_url"]},
                    "fetch_mode": "PUBLIC_HTTPS", "monitoring_source_id": source["id"],
                    "monitoring_revision": monitoring["revision"],
                    "trigger_reason": "KNOWN_SOURCE_BROKEN",
                    "request_id": "fixture-failed-source", "profile_id": None,
                }, connection=connection)
                connection.execute(update(m.company_source_monitoring).where(
                    m.company_source_monitoring.c.source_id == source["id"],
                ).values(status="FAILED", failure_count=3, last_error="HTTP_STATUS_ERROR",
                         rediscovery_operation_id=operation_id))

            app = create_app(store, "http://127.0.0.1:1", root / "outputs",
                             redis_url="redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external, errors = [], []
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated monitoring API did not start")
                    time.sleep(0.02)
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
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.locator("#active-profile").select_option("fixture")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    card = page.locator(f'[data-source-id="{source["id"]}"]')
                    expect(card).to_contain_text("Failed")
                    expect(card).to_contain_text("3 (automatic retries stopped)")
                    expected_notice = "Repeated failures queued an employer-source rediscovery"
                    expect(card.get_by_role("status").filter(has_text=expected_notice)).to_be_visible()
                    expect(card).to_contain_text("existing failed source and its settings remain unchanged")

                    page.reload(wait_until="domcontentloaded")
                    page.locator("#active-profile").select_option("fixture")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    card = page.locator(f'[data-source-id="{source["id"]}"]')
                    expect(card.get_by_role("status").filter(has_text=expected_notice)).to_be_visible()
                    with store.engine.connect() as connection:
                        current_source = connection.execute(select(m.company_sources).where(
                            m.company_sources.c.id == source["id"])).mappings().one()
                        current_monitor = connection.execute(select(m.company_source_monitoring).where(
                            m.company_source_monitoring.c.source_id == source["id"])).mappings().one()
                    self.assertEqual(current_source["endpoint"], "https://employer.example/jobs.json")
                    self.assertTrue(current_source["enabled"])
                    self.assertEqual(current_monitor["status"], "FAILED")
                    self.assertEqual(current_monitor["rediscovery_operation_id"], operation_id)
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated test API must stop")


if __name__ == "__main__":
    unittest.main()
