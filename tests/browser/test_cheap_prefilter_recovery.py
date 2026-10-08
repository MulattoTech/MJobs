"""Connected FIXTURE browser checks for cheap prefilter preview recovery."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit
import unittest

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
SEARCH_ID = "cheap-prefilter-recovery-search"
PREVIEW_PATH = f"/profiles/fixture/search-profiles/{SEARCH_ID}/cheap-prefilter-preview"


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class CheapPrefilterRecoveryBrowserTests(unittest.TestCase):
    def test_preview_error_keeps_saved_criteria_and_captured_jobs(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-cheap-prefilter-recovery-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(deepcopy(PROFILE))
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "prefilter-recovery-employer",
                    "name": "Prefilter Recovery Fixture",
                    "domain": "prefilter-recovery.fixture.invalid",
                })
            endpoint = "https://prefilter-recovery.fixture.invalid/jobs.json"
            source = upsert_company_job_source(
                store, company_id="prefilter-recovery-employer", provider="rss",
                tenant_identifier="prefilter-recovery-fixture", public_api_url=endpoint,
                collection_strategy="PUBLIC_JSON",
            )
            import_company_feed_jobs(
                store, company_id="prefilter-recovery-employer", source_id=source["id"],
                provider="rss", tenant="prefilter-recovery-fixture", endpoint=endpoint,
                strategy="PUBLIC_JSON", profile_id="fixture", fetch_available=True,
                items=[
                    {"id": "linux-admin", "title": "Systems Administrator",
                     "description": "Required: Linux server administration.",
                     "location": "Remote", "url": "https://prefilter-recovery.fixture.invalid/apply/linux-admin"},
                    {"id": "windows-admin", "title": "Systems Administrator",
                     "description": "Required: Windows server administration.",
                     "location": "Remote", "url": "https://prefilter-recovery.fixture.invalid/apply/windows-admin"},
                    {"id": "linux-network", "title": "Network Administrator",
                     "description": "Required: Linux server administration.",
                     "location": "Remote", "url": "https://prefilter-recovery.fixture.invalid/apply/linux-network"},
                ],
            )
            # The preview route reads raw postings by the selected configured-source ID.
            # Mirror the feed's posting source as a fixture-owned source so the saved
            # search explicitly scopes its preview to those durable rows.
            selected_source_id = f"company-feed:{source['id']}"
            with store.engine.begin() as connection:
                upsert(connection, m.job_sources, {
                    "id": selected_source_id,
                    "provider": "rss",
                    "tenant": "prefilter-recovery-fixture",
                    "name": "Prefilter Recovery Fixture",
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
            save_search_profile(store, {
                "id": SEARCH_ID,
                "profile_id": "fixture",
                "name": "Recovery systems administrator",
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
                configured_source = connection.execute(select(m.job_sources).where(
                    m.job_sources.c.id == selected_source_id)).mappings().one_or_none()
                saved_source_ids = connection.execute(select(m.search_profiles.c.source_ids).where(
                    m.search_profiles.c.id == SEARCH_ID)).scalar_one()
                posting_sources = set(connection.execute(select(m.postings.c.source).distinct()).scalars())
                original_postings = [dict(row) for row in connection.execute(
                    select(m.postings).order_by(m.postings.c.id)).mappings()]
            self.assertIsNotNone(configured_source)
            self.assertEqual(saved_source_ids, [selected_source_id])
            self.assertEqual(posting_sources, {selected_source_id})
            self.assertEqual(len(original_postings), 3)
            store.engine.dispose()
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
                        self.fail("Isolated prefilter recovery API did not start")
                    time.sleep(0.02)

                external = []
                preview_attempts = 0
                application_or_ai_posts = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        nonlocal preview_attempts
                        request = route.request
                        if urlsplit(request.url).netloc != urlsplit(origin).netloc:
                            external.append(request.url)
                            route.abort()
                            return
                        if request.method == "POST" and (
                                "/applications" in request.url or "/ai/" in request.url
                                or "evaluation" in request.url):
                            application_or_ai_posts.append(request.url)
                        if request.url == origin + PREVIEW_PATH:
                            preview_attempts += 1
                            if preview_attempts == 2:
                                route.fulfill(
                                    status=503,
                                    content_type="application/json",
                                    body='{"detail":"synthetic prefilter preview failure"}',
                                )
                                return
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_label("Saved search profile", exact=True).select_option(SEARCH_ID)
                    expect(page.get_by_label("Included job titles", exact=True)).to_have_value(
                        "Systems Administrator")
                    expect(page.get_by_label("Required keywords", exact=True)).to_have_value("Linux")

                    with page.expect_response(lambda response:
                            response.url == origin + PREVIEW_PATH) as first_preview:
                        page.get_by_role(
                            "button", name="Preview title and keyword filter", exact=True
                        ).click()
                    self.assertEqual(first_preview.value.status, 200)
                    expect(page.get_by_role("region", name="Prefilter stage counts")).to_contain_text(
                        "Captured: 3")
                    expect(page.locator(".job-card")).to_have_count(3)
                    preserved_cards = page.locator(".job-card").all_text_contents()

                    with page.expect_response(lambda response:
                            response.url == origin + PREVIEW_PATH) as failed_preview:
                        page.get_by_role(
                            "button", name="Preview title and keyword filter", exact=True
                        ).click()
                    self.assertEqual(failed_preview.value.status, 503)
                    expect(page.get_by_role("alert")).to_be_visible()
                    expect(page.get_by_label("Saved search profile", exact=True)).to_have_value(SEARCH_ID)
                    expect(page.get_by_label("Included job titles", exact=True)).to_have_value(
                        "Systems Administrator")
                    expect(page.get_by_label("Required keywords", exact=True)).to_have_value("Linux")
                    expect(page.locator(".job-card")).to_have_count(3)
                    self.assertEqual(page.locator(".job-card").all_text_contents(), preserved_cards)

                    # The failed preview does not dirty persisted criteria or source rows.
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_label("Saved search profile", exact=True).select_option(SEARCH_ID)
                    expect(page.get_by_label("Included job titles", exact=True)).to_have_value(
                        "Systems Administrator")
                    expect(page.get_by_label("Required keywords", exact=True)).to_have_value("Linux")
                    expect(page.locator(".job-card")).to_have_count(3)
                    self.assertEqual(preview_attempts, 2)
                    self.assertEqual(external, [])
                    self.assertEqual(application_or_ai_posts, [])
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
                self.assertEqual(store.count("applications"), 0)
                self.assertEqual(store.count("ai_evaluation_runs"), 0)
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated prefilter recovery API did not stop")


if __name__ == "__main__":
    unittest.main()
