"""Connected browser acceptance for persisted prefilter criteria and discovery preview."""
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
from packages.persistence.store import Store, identity, now, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]
SEARCH_ID = "prefilter-discovery-flow-search"
PREVIEW_PATH = f"/profiles/fixture/search-profiles/{SEARCH_ID}/cheap-prefilter-preview"


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class PrefilterDiscoveryFlowBrowserTests(unittest.TestCase):
    def test_saved_criteria_preview_counts_and_preserved_discovery_rows(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-prefilter-discovery-flow-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "prefilter-flow-employer",
                    "name": "Prefilter Discovery Fixture",
                    "domain": "prefilter-flow.fixture.invalid",
                })

            endpoint = "https://prefilter-flow.fixture.invalid/jobs.json"
            company_source = upsert_company_job_source(
                store, company_id="prefilter-flow-employer", provider="rss",
                tenant_identifier="prefilter-flow-fixture", public_api_url=endpoint,
                collection_strategy="PUBLIC_JSON",
            )
            import_company_feed_jobs(
                store, company_id="prefilter-flow-employer", source_id=company_source["id"],
                provider="rss", tenant="prefilter-flow-fixture", endpoint=endpoint,
                strategy="PUBLIC_JSON", profile_id="fixture", fetch_available=True,
                items=[
                    {"id": "passing-systems-admin", "title": "Systems Administrator",
                     "description": "Required: Linux server administration.",
                     "location": "Remote", "url": "https://prefilter-flow.fixture.invalid/apply/pass"},
                    {"id": "rejected-title", "title": "Network Administrator",
                     "description": "Required: Linux network administration.",
                     "location": "Remote", "url": "https://prefilter-flow.fixture.invalid/apply/title"},
                    {"id": "rejected-required-keyword", "title": "Systems Administrator",
                     "description": "Required: Windows server administration.",
                     "location": "Remote", "url": "https://prefilter-flow.fixture.invalid/apply/keyword"},
                    {"id": "rejected-excluded-title", "title": "Systems Administrator Intern",
                     "description": "Required: Linux server administration.",
                     "location": "Remote", "url": "https://prefilter-flow.fixture.invalid/apply/intern"},
                    {"id": "rejected-excluded-keyword", "title": "Systems Administrator",
                     "description": "Required: Linux server administration. Commission based.",
                     "location": "Remote", "url": "https://prefilter-flow.fixture.invalid/apply/commission"},
                ],
            )

            # The preview endpoint scopes raw postings by the selected configured-source ID.
            selected_source_id = f"company-feed:{company_source['id']}"
            with store.engine.begin() as connection:
                upsert(connection, m.job_sources, {
                    "id": selected_source_id,
                    "provider": "rss",
                    "tenant": "prefilter-flow-fixture",
                    "name": "Prefilter Discovery Fixture",
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

            with store.engine.connect() as connection:
                good_job = next(dict(row) for row in connection.execute(
                    select(m.postings).order_by(m.postings.c.id)).mappings()
                    if row["raw"].get("id") == "passing-systems-admin")
            # Add enough additional plausible raw rows to exercise the preview's 500-row
            # candidate bound without making 500 duplicate canonical jobs in the UI.
            with store.engine.begin() as connection:
                for index in range(500):
                    external_id = f"passing-deferred-{index:04d}"
                    upsert(connection, m.postings, {
                        "id": identity("prefilter-discovery-flow", external_id),
                        "job_id": good_job["job_id"],
                        "source": selected_source_id,
                        "source_job_id": external_id,
                        "source_url": endpoint,
                        "application_url": good_job["application_url"],
                        "raw": {
                            "id": external_id,
                            "title": "Systems Administrator",
                            "description": "Required: Linux server administration.",
                            "location": "Remote",
                            "url": good_job["application_url"],
                        },
                        "fetched_at": now(),
                    })

            save_search_profile(store, {
                "id": SEARCH_ID,
                "profile_id": "fixture",
                "name": "Prefilter discovery flow",
                "enabled": True,
                "include_titles": [],
                "exclude_titles": [],
                "required_keywords": [],
                "excluded_keywords": [],
                "exact_phrases": [],
                "optional_keywords": [],
                "employment_types": [],
                "source_ids": [selected_source_id],
            })

            with store.engine.connect() as connection:
                original_postings = [dict(row) for row in connection.execute(
                    select(m.postings).order_by(m.postings.c.id)).mappings()]
                source_ids = connection.execute(select(m.search_profiles.c.source_ids).where(
                    m.search_profiles.c.id == SEARCH_ID)).scalar_one()
            self.assertEqual(len(original_postings), 505)
            self.assertEqual(source_ids, [selected_source_id])
            store.engine.dispose()
            # Reopen before the browser so persisted criteria and postings are authoritative.
            store = Store(database_url)
            operations_before = store.count("worker_operations")
            applications_before = store.count("applications")
            ai_runs_before = store.count("ai_evaluation_runs")

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
                        self.fail("Isolated prefilter discovery API did not start")
                    time.sleep(0.02)

                external_requests = []
                application_or_ai_posts = []
                preview_requests = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        request = route.request
                        if urlsplit(request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                            return
                        if request.method == "POST" and (
                                "/applications" in request.url or "/mock-run" in request.url
                                or "/ai/" in request.url or "evaluation" in request.url):
                            application_or_ai_posts.append(request.url)
                        if request.url == origin + PREVIEW_PATH:
                            preview_requests.append(request.post_data_json)
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_label("Saved search profile", exact=True).select_option(SEARCH_ID)

                    page.get_by_label("Included job titles", exact=True).fill("Systems Administrator")
                    page.get_by_label("Excluded job titles", exact=True).fill("Intern")
                    page.get_by_label("Required keywords", exact=True).fill("Linux")
                    page.get_by_label("Excluded keywords", exact=True).fill("commission")
                    page.get_by_role("button", name="Save title and keyword filters", exact=True).click()

                    # The saved criteria must be durable before requesting a preview.
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_label("Saved search profile", exact=True).select_option(SEARCH_ID)
                    expect(page.get_by_label("Included job titles", exact=True)).to_have_value(
                        "Systems Administrator")
                    expect(page.get_by_label("Excluded job titles", exact=True)).to_have_value("Intern")
                    expect(page.get_by_label("Required keywords", exact=True)).to_have_value("Linux")
                    expect(page.get_by_label("Excluded keywords", exact=True)).to_have_value("commission")

                    with page.expect_response(lambda response:
                            response.url == origin + PREVIEW_PATH) as preview_response:
                        page.get_by_role(
                            "button", name="Preview title and keyword filter", exact=True
                        ).click()
                    self.assertEqual(preview_response.value.status, 200)
                    request_criteria = preview_requests[-1]["criteria"]
                    self.assertEqual(request_criteria, {
                        "include_titles": ["Systems Administrator"],
                        "exclude_titles": ["Intern"],
                        "required_keywords": ["Linux"],
                        "excluded_keywords": ["commission"],
                        "exact_phrases": [],
                    })
                    preview = preview_response.value.json()
                    self.assertEqual(preview["stage_counts"], {"captured": 505, "prefiltered": 500})
                    self.assertEqual(preview["candidate_count"], 500)
                    self.assertEqual(preview["rejected_count"], 4)
                    self.assertEqual(preview["deferred_count"], 1)
                    counts = page.get_by_role("region", name="Prefilter stage counts")
                    expect(counts).to_contain_text("Captured: 505")
                    expect(counts).to_contain_text("Prefiltered candidates: 500")
                    expect(counts).to_contain_text("Rejected by prefilter: 4")
                    expect(counts).to_contain_text("Deferred by candidate limit: 1")

                    # Preview is not destructive: clearing the selected search still exposes
                    # every distinct captured canonical job, including rejected examples.
                    page.get_by_label("Saved search profile", exact=True).select_option("")
                    expect(page.locator(".job-card")).to_have_count(5)
                    cards = page.locator(".job-card").all_text_contents()
                    self.assertTrue(any("Network Administrator" in card for card in cards))
                    self.assertTrue(any("Systems Administrator Intern" in card for card in cards))
                    self.assertEqual(len(preview_requests), 1)
                    self.assertEqual(external_requests, [])
                    self.assertEqual(application_or_ai_posts, [])
                    self.assertEqual(store.count("worker_operations"), operations_before)
                    self.assertEqual(store.count("applications"), applications_before)
                    self.assertEqual(store.count("ai_evaluation_runs"), ai_runs_before)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                with store.engine.connect() as connection:
                    persisted_postings = [dict(row) for row in connection.execute(
                        select(m.postings).order_by(m.postings.c.id)).mappings()]
                self.assertEqual(persisted_postings, original_postings)
                persisted_raw_ids = {row["raw"].get("id") for row in persisted_postings}
                self.assertTrue({"rejected-title", "rejected-required-keyword",
                                 "rejected-excluded-title", "rejected-excluded-keyword",
                                 "passing-deferred-0499"}.issubset(persisted_raw_ids))
                self.assertEqual(store.count("worker_operations"), operations_before)
                self.assertEqual(store.count("applications"), applications_before)
                self.assertEqual(store.count("ai_evaluation_runs"), ai_runs_before)
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated prefilter discovery API did not stop")


if __name__ == "__main__":
    unittest.main()
