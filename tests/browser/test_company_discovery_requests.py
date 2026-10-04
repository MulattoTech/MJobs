"""Real browser proof for durable company discovery requests and explicit review."""
import copy
import os
from pathlib import Path
from fixtures.browser_ports import bound_browser_socket
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

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.requests import list_discovery_requests
from services.discovery.sources.collect import collect_source, configure_source
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class CompanyDiscoveryRequestsBrowserTests(unittest.TestCase):
    def test_real_collection_request_review_worker_and_identity_separation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'requests.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            source_id = configure_source(store, "example")
            original_company_id = identity("greenhouse", "example")

            def board_transport(request):
                if request.url.path.endswith("/jobs"):
                    return httpx.Response(200, json={"jobs": [RAW], "meta": {"total": 1}})
                return httpx.Response(200, json={"name": "Example Public Employer"})

            with patch.object(PublicCompanyClient, "get", side_effect=AssertionError("Collection must not use employer discovery")):
                collect_source(store, source_id, "fixture", transport=httpx.MockTransport(board_transport))
            request = list_discovery_requests(store)[0]
            self.assertEqual(request["status"], "NEEDS_ORIGIN_REVIEW")
            self.assertIn("PREVIOUSLY_UNSEEN_COMPANY", request["reasons"])
            self.assertIn("HIGH_MATCH_JOB", request["reasons"])
            self.assertEqual(request["company_name"], "Example Public Employer")
            with store.engine.connect() as connection:
                original_job = connection.execute(select(m.jobs).where(m.jobs.c.company_id == original_company_id)).mappings().one()
                original_route = connection.execute(select(m.routes).where(m.routes.c.id == original_job["id"])).mappings().one()

            pages = {
                "/robots.txt": "User-agent: *\nAllow: /",
                "/": '<html>Example Public Employer<a rel="careers" href="/careers">Careers</a></html>',
                "/careers": '<html>Example Public Employer<link rel="alternate" type="application/feed+json" href="/jobs.json"></html>',
                "/jobs.json": '{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"role-1","url":"https://employer.example/jobs/1","title":"Python Engineer","content_text":"Python work.","location":"Remote - United States"}]}',
                "/jobs/1": '<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","identifier":{"@type":"PropertyValue","value":"role-1"},"title":"Python Engineer","url":"https://employer.example/jobs/1"}</script>',
            }
            seen = []

            def fetch(client, url, deadline):
                parsed = urlsplit(url)
                seen.append(url)
                if parsed.path not in pages:
                    raise AssertionError(f"Unexpected synthetic fetch: {url}")
                content_type = "application/json" if parsed.path.endswith(".json") else "text/html; charset=utf-8"
                return httpx.Response(200, text=pages[parsed.path], headers={"content-type": content_type}, request=httpx.Request("GET", url))

            env = {"MJOBS_DATABASE_URL": store.url, "MJOBS_OPERATION_MODE": "PERSONAL",
                   "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0", "MJOBS_OUTPUT_DIR": str(root / "outputs")}
            app = create_app(store, "http://127.0.0.1:1", root / "outputs",
                             redis_url="redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            worker_thread = None
            worker_result = {}
            try:
                with patch.dict(os.environ, env), patch.object(PublicCompanyClient, "_request_once", fetch):
                    thread.start()
                    deadline = time.monotonic() + 10
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
                        page.get_by_role("button", name="Sources", exact=True).click()
                        card = page.locator("article[aria-label$='discovery request']").filter(has_text="Example Public Employer").first
                        expect(card).to_be_visible()
                        expect(card).to_contain_text("Company not seen before")
                        expect(card).to_contain_text("Strong job match")
                        expect(card).to_contain_text("Reviewing a site does not verify employer ownership")
                        card.get_by_label("Website to review (HTTPS)").fill("https://employer.example/")
                        with page.expect_response(lambda response: response.request.method == "POST" and "/company-discovery/requests/" in response.url and response.url.endswith("/review")) as review_response:
                            card.get_by_role("button", name="Review website and discover", exact=True).click()
                        operation_id = review_response.value.json()["operation_id"]
                        self.assertTrue(operation_id)

                        def run_worker():
                            worker_result["value"] = execute_operation(operation_id)

                        worker_thread = threading.Thread(target=run_worker, daemon=True)
                        worker_thread.start()
                        expect(page.get_by_role("status").filter(has_text="Completed")).to_be_visible(timeout=15000)
                        refreshed = page.locator("article[aria-label$='discovery request']").filter(has_text="Example Public Employer").first
                        expect(refreshed).to_contain_text("Completed")
                        self.assertEqual(errors, [])
                        self.assertEqual(external, [])
                        context.close()
                        browser.close()
                    worker_thread.join(timeout=10)
                    self.assertFalse(worker_thread.is_alive(), "Discovery request worker did not finish")
                    self.assertEqual(worker_result.get("value", {}).get("status"), "COMPLETED", worker_result)
                self.assertEqual(set(seen), {"https://employer.example" + path for path in pages})
                with store.engine.connect() as connection:
                    companies = list(connection.execute(select(m.companies)).mappings())
                    jobs = list(connection.execute(select(m.jobs)).mappings())
                    routes = list(connection.execute(select(m.routes)).mappings())
                    current = connection.execute(select(m.company_discovery_requests)).mappings().one()
                self.assertEqual(list_discovery_requests(store)[0]["status"], "COMPLETED")
                self.assertEqual(len(jobs), 2)
                unchanged = next(job for job in jobs if job["id"] == original_job["id"])
                self.assertEqual(unchanged["company_id"], original_job["company_id"])
                self.assertEqual(unchanged["title"], original_job["title"])
                unchanged_route = next(route for route in routes if route["id"] == original_route["id"])
                self.assertEqual(unchanged_route["url"], original_route["url"])
                imported = [job for job in jobs if job["id"] != original_job["id"]]
                self.assertEqual(len(imported), 1)
                self.assertNotEqual(imported[0]["company_id"], original_company_id)
                self.assertIn(imported[0]["company_id"], {row["id"] for row in companies})
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated test API must stop")


if __name__ == "__main__":
    unittest.main()
