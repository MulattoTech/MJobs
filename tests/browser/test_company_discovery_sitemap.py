"""Connected browser acceptance for same-origin sitemap company discovery."""
import copy
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from fixtures.browser_ports import bound_browser_socket
from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.public_client import PublicCompanyClient
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class CompanyDiscoverySitemapBrowserTests(unittest.TestCase):
    def test_user_discovers_sitemap_feed_and_reads_job_back_through_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'sitemap-browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            env = {
                "MJOBS_DATABASE_URL": store.url,
                "MJOBS_OPERATION_MODE": "PERSONAL",
                "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
                "MJOBS_OUTPUT_DIR": str(root / "outputs"),
            }
            # The employer has no career link in its rendered home page. The
            # sitemap is the only path to the career page and public feed.
            pages = {
                "/robots.txt": "User-agent: *\nAllow: /",
                "/": '<html><title>Example Systems</title><p>Example Systems builds software.</p>'
                     '<link rel="sitemap" type="application/xml" href="/sitemap.xml"></html>',
                "/sitemap.xml": '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                '<url><loc>https://employer.example/team/careers/</loc></url></urlset>',
                "/team/careers/": '<html><title>Example Systems Careers</title>'
                                   '<link rel="alternate" type="application/feed+json" href="/public/openings.json">'
                                   '</html>',
                "/public/openings.json": '{"version":"https://jsonfeed.org/version/1.1","items":['
                                         '{"id":"sitemap-role-1","url":"https://employer.example/jobs/sitemap-role-1",'
                                         '"title":"Python Systems Engineer","content_text":"Python platform work. $92,000 per year.",'
                                         '"location":"Remote - United States"}]}',
                "/jobs/sitemap-role-1": '<script type="application/ld+json">{"@context":"https://schema.org",'
                                         '"@type":"JobPosting","identifier":{"@type":"PropertyValue",'
                                         '"value":"sitemap-role-1"},"title":"Python Systems Engineer",'
                                         '"url":"https://employer.example/jobs/sitemap-role-1"}</script>',
            }
            seen = []

            def fetch(client, url, deadline):
                parsed = urlsplit(url)
                seen.append(url)
                if parsed.scheme != "https" or parsed.netloc != "employer.example":
                    raise AssertionError(f"Unexpected external host fetch: {url}")
                if parsed.path not in pages:
                    raise AssertionError(f"Unexpected synthetic employer fetch: {url}")
                content_type = "application/json" if parsed.path.endswith(".json") else (
                    "application/xml" if parsed.path.endswith(".xml") else "text/html; charset=utf-8")
                request = httpx.Request("GET", url)
                return httpx.Response(200, text=pages[parsed.path],
                                      headers={"content-type": content_type}, request=request)

            app = create_app(store, "http://127.0.0.1:1", root / "outputs",
                             redis_url="redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            worker_thread = None
            worker_result = {}
            operation_id = None
            try:
                with patch.dict(os.environ, env), \
                        patch.object(PublicCompanyClient, "_request_once", fetch), \
                        patch("services.company_discovery.flywheel.search_company_careers",
                              side_effect=AssertionError("A supplied employer site must not call a search provider")):
                    thread.start()
                    deadline = time.monotonic() + 10
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            self.fail("Isolated browser API did not start")
                        time.sleep(0.02)

                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context(viewport={"width": 1365, "height": 1000})
                        external_browser_requests = []

                        def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external_browser_requests.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page_errors = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(origin, wait_until="domcontentloaded")
                        page.locator("#active-profile").select_option("fixture")
                        page.get_by_role("button", name="Sources", exact=True).click()
                        form = page.get_by_role("heading", name="Add a company to look for jobs", exact=True).locator("..")
                        form.get_by_label("Company name").fill("Example Systems")
                        form.get_by_label("Company website (HTTPS)").fill("https://employer.example/")
                        with page.expect_response(lambda response: response.request.method == "POST"
                                                  and response.url.endswith("/companies/discovery")) as queued_response:
                            form.get_by_role("button", name="Discover company jobs", exact=True).click()
                        operation_id = queued_response.value.json()["operation_id"]
                        self.assertTrue(operation_id)

                        def run_worker():
                            worker_result["value"] = execute_operation(operation_id)

                        worker_thread = threading.Thread(target=run_worker, daemon=True)
                        worker_thread.start()
                        expect(page.get_by_role("status").filter(has_text="Completed")).to_be_visible(timeout=15000)
                        expect(page.get_by_role("status").filter(
                            has_text="Public company jobs and source information have been updated.")).to_be_visible()

                        page.get_by_role("button", name=re.compile(r"^Jobs")).click()
                        job = page.locator(".job-card").filter(has_text="Example Systems").first
                        expect(job).to_be_visible()
                        expect(job).to_contain_text("Python Systems Engineer")

                        page.get_by_role("button", name="Sources", exact=True).click()
                        source = page.locator(".company-source-card").filter(has_text="Example Systems").first
                        expect(source).to_be_visible()
                        expect(source).to_contain_text("Jobs Found")
                        expect(source).to_contain_text("1 sampled posting observations across 1 accepted feed row")

                        # A full browser reload forces the app to read persisted
                        # source and job state back through its normal API.
                        page.reload(wait_until="domcontentloaded")
                        page.get_by_role("button", name=re.compile(r"^Jobs")).click()
                        persisted_job = page.locator(".job-card").filter(has_text="Example Systems").first
                        expect(persisted_job).to_be_visible()
                        expect(persisted_job).to_contain_text("Python Systems Engineer")
                        page.get_by_role("button", name="Sources", exact=True).click()
                        persisted_source = page.locator(".company-source-card").filter(
                            has_text="Example Systems").first
                        expect(persisted_source).to_be_visible()
                        expect(persisted_source).to_contain_text("1 sampled posting observations across 1 accepted feed row")

                        self.assertEqual(page_errors, [])
                        self.assertEqual(external_browser_requests, [])
                        context.close()
                        browser.close()

                    worker_thread.join(timeout=10)
                    self.assertFalse(worker_thread.is_alive(), "Company-discovery worker did not finish")
                    self.assertEqual(worker_result.get("value", {}).get("status"), "COMPLETED", worker_result)
                self.assertEqual(set(seen), {"https://employer.example" + path for path in pages})
                with store.engine.connect() as connection:
                    source = connection.execute(select(m.company_sources)).mappings().one()
                    job = connection.execute(select(m.jobs)).mappings().one()
                    operation = connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id)).mappings().one()
                # Company source URLs use the existing canonical form, which
                # strips a non-root trailing slash after the actual fetch.
                self.assertEqual(source["career_url"], "https://employer.example/team/careers")
                self.assertEqual(job["title"], "Python Systems Engineer")
                self.assertEqual(operation["state"], "COMPLETED")
            finally:
                if worker_thread is not None:
                    worker_thread.join(timeout=10)
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated test API must stop")


if __name__ == "__main__":
    unittest.main()
