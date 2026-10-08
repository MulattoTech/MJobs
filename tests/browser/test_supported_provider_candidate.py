"""Connected browser acceptance for supported-feed source candidates."""
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
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs
from tests.integration.test_spine import PROFILE


class SupportedProviderCandidateBrowserTests(unittest.TestCase):
    def test_supported_collection_candidate_is_durable_profile_scoped_and_not_recollected_by_ui(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-supported-candidate-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'supported-candidate.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                "email": "other@example.invalid"})
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "supported-feed-candidate-employer", "name": "Synthetic Feed Employer",
                    "domain": "employer.example",
                })
            endpoint = "https://employer.example/jobs.json"
            source = upsert_company_job_source(
                store, company_id="supported-feed-candidate-employer", provider="json_feed",
                tenant_identifier="synthetic-supported-feed", public_api_url=endpoint,
                collection_strategy="PUBLIC_JSON",
            )
            items = [{
                "id": "synthetic-platform-role", "title": "Synthetic Platform Engineer",
                "description": "Maintain synthetic application systems.",
                "url": "https://jobs.example-ats.invalid/apply/role-1",
            }]

            # Two identical fixture collection events exercise the durable idempotency key.
            for _ in range(2):
                self.assertEqual(import_company_feed_jobs(
                    store, company_id="supported-feed-candidate-employer", source_id=source["id"],
                    provider="json_feed", tenant="synthetic-supported-feed", endpoint=endpoint,
                    strategy="PUBLIC_JSON", profile_id="fixture", items=items,
                ), 1)

            with store.engine.connect() as connection:
                candidates = connection.execute(select(m.source_candidates)).mappings().all()
                observations = connection.execute(
                    select(m.source_candidate_observations)
                ).mappings().all()
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["profile_id"], "fixture")
            self.assertEqual(candidates[0]["domain"], "jobs.example-ats.invalid")
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0]["profile_id"], "fixture")
            self.assertEqual(observations[0]["capture_method"], "SUPPORTED_COLLECTION")

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
                        self.fail("Isolated supported-candidate API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    outbound = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            outbound.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.locator("#active-profile").select_option("fixture")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    candidate = page.get_by_role(
                        "article", name="Source candidate jobs.example-ats.invalid")
                    expect(candidate).to_be_visible()
                    expect(candidate).to_contain_text("SUPPORTED COLLECTION")
                    expect(candidate).to_contain_text(endpoint)
                    expect(candidate).to_contain_text(
                        "https://jobs.example-ats.invalid/apply/role-1")
                    expect(candidate).to_contain_text("Collection disabled")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    candidates_after_reload = page.get_by_role(
                        "article", name="Source candidate jobs.example-ats.invalid")
                    expect(candidates_after_reload).to_have_count(1)
                    expect(candidates_after_reload).to_contain_text("SUPPORTED COLLECTION")
                    page.locator("#active-profile").select_option("other")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(page.get_by_text(
                        "No third-party source candidates are recorded for this profile.", exact=True
                    )).to_be_visible()
                    self.assertEqual(outbound, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated test API must stop")


if __name__ == "__main__":
    unittest.main()
