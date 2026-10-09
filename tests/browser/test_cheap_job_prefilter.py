"""Connected FIXTURE browser acceptance for the persisted cheap job prefilter."""
from copy import deepcopy
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
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class CheapJobPrefilterBrowserTests(unittest.TestCase):
    def test_saved_profile_prefilter_narrows_candidate_work_without_changing_postings(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-cheap-prefilter-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "cheap-prefilter-employer",
                    "name": "Prefilter Fixture Employer",
                    "domain": "prefilter.fixture.invalid",
                })
            endpoint = "https://prefilter.fixture.invalid/jobs.json"
            source = upsert_company_job_source(
                store, company_id="cheap-prefilter-employer", provider="rss",
                tenant_identifier="prefilter-fixture", public_api_url=endpoint,
                collection_strategy="PUBLIC_JSON",
            )
            selected_source_id = f"company-feed:{source['id']}"
            with store.engine.begin() as connection:
                upsert(connection, m.job_sources, {
                    "id": selected_source_id,
                    "provider": "rss",
                    "tenant": "prefilter-fixture",
                    "name": "Prefilter Fixture Employer",
                    "enabled": True,
                    "strategy": "PUBLIC_JSON",
                    "capabilities": {"collection_strategy": "PUBLIC_JSON"},
                    "poll_interval": 300,
                    "concurrency": 1,
                    "request_timeout": 10,
                    "rate_limit": 60,
                    "jitter_seconds": 0,
                    "health_status": "NOT_CHECKED",
                })
            import_company_feed_jobs(
                store, company_id="cheap-prefilter-employer", source_id=source["id"],
                provider="rss", tenant="prefilter-fixture", endpoint=endpoint,
                strategy="PUBLIC_JSON", profile_id="fixture", fetch_available=True,
                items=[
                    {"id": "linux-admin", "title": "Systems Administrator",
                     "description": "Required: Linux server administration.",
                     "location": "Remote", "url": "https://prefilter.fixture.invalid/apply/linux-admin"},
                    {"id": "windows-admin", "title": "Systems Administrator",
                     "description": "Required: Windows server administration.",
                     "location": "Remote", "url": "https://prefilter.fixture.invalid/apply/windows-admin"},
                    {"id": "linux-network", "title": "Network Administrator",
                     "description": "Required: Linux server administration.",
                     "location": "Remote", "url": "https://prefilter.fixture.invalid/apply/linux-network"},
                ],
            )
            save_search_profile(store, {
                "id": "cheap-prefilter-search",
                "profile_id": "fixture",
                "name": "Linux systems administrator",
                "enabled": True,
                "include_titles": ["Systems Administrator"],
                "required_keywords": ["Linux"],
                "exclude_titles": [],
                "excluded_keywords": [],
                "exact_phrases": [],
                "optional_keywords": [],
                "employment_types": [],
                "source_ids": [selected_source_id],
            })

            with store.engine.connect() as connection:
                original_postings = [dict(row) for row in connection.execute(
                    select(m.postings).order_by(m.postings.c.id)).mappings()]
            self.assertEqual(len(original_postings), 3)
            self.assertEqual(store.count("applications"), 0)
            store.engine.dispose()
            # The browser uses a reopened store to prove criteria and postings are durable.
            store = Store(database_url)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated prefilter API did not start")
                    time.sleep(0.02)

                external = []
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
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")

                    search = page.get_by_label("Saved search profile", exact=True)
                    expect(search.locator("option", has_text="Linux systems administrator")).to_have_count(1)
                    search.select_option("cheap-prefilter-search")

                    expect(page.get_by_label("Included job titles", exact=True)).to_have_value(
                        "Systems Administrator")
                    expect(page.get_by_label("Required keywords", exact=True)).to_have_value("Linux")
                    expect(page.get_by_text(
                        "Captured postings are retained. These filters only narrow current candidate work.",
                        exact=True)).to_be_visible()

                    with page.expect_response(
                            lambda response: response.url == (
                                origin + "/profiles/fixture/search-profiles/"
                                "cheap-prefilter-search/cheap-prefilter-preview"
                            )) as preview_response:
                        page.get_by_role("button", name="Preview title and keyword filter", exact=True).click()
                    self.assertEqual(preview_response.value.status, 200)
                    self.assertEqual(preview_response.value.request.post_data_json, {
                        "criteria": {
                            "include_titles": ["Systems Administrator"],
                            "exclude_titles": [],
                            "required_keywords": ["Linux"],
                            "excluded_keywords": [],
                            "exact_phrases": [],
                        },
                    })
                    preview = preview_response.value.json()
                    self.assertEqual(preview["profile_id"], "fixture")
                    self.assertEqual(preview["search_profile_id"], "cheap-prefilter-search")
                    self.assertEqual(preview["stage_counts"], {"captured": 3, "prefiltered": 1})
                    self.assertEqual(preview["candidate_count"], 1)
                    self.assertEqual(preview["rejected_count"], 2)
                    self.assertEqual(preview["deferred_count"], 0)
                    counts = page.get_by_role("region", name="Prefilter stage counts")
                    expect(counts).to_contain_text("Captured: 3")
                    expect(counts).to_contain_text("Prefiltered candidates: 1")
                    expect(counts).to_contain_text("Rejected by prefilter: 2")
                    expect(counts).to_contain_text("Deferred by candidate limit: 0")

                    # The saved criteria survive reload and remain attached to the selected profile.
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_label("Saved search profile", exact=True).select_option(
                        "cheap-prefilter-search")
                    expect(page.get_by_label("Included job titles", exact=True)).to_have_value(
                        "Systems Administrator")
                    expect(page.get_by_label("Required keywords", exact=True)).to_have_value("Linux")

                    self.assertEqual(external, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                with store.engine.connect() as connection:
                    persisted_postings = [dict(row) for row in connection.execute(
                        select(m.postings).order_by(m.postings.c.id)).mappings()]
                self.assertEqual(persisted_postings, original_postings)
                self.assertEqual(store.count("applications"), 0)
                self.assertEqual(store.count("ai_evaluation_runs"), 0)
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated prefilter API did not stop")


if __name__ == "__main__":
    unittest.main()
