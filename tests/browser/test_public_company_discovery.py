"""Real browser proof for PERSONAL public-company discovery and worker execution."""
import copy
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch
import re

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.public_client import PublicCompanyClient
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class PublicCompanyDiscoveryBrowserTests(unittest.TestCase):
    def test_personal_form_posts_and_real_worker_imports_review_required_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            env = {
                "MJOBS_DATABASE_URL": store.url,
                "MJOBS_OPERATION_MODE": "PERSONAL",
                "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
                "MJOBS_OUTPUT_DIR": str(root / "outputs"),
            }
            pages = {
                "/robots.txt": "User-agent: *\nAllow: /",
                "/": '<html>Example Systems<a rel="careers" href="/careers">Careers</a></html>',
                "/careers": '<html>Example Systems<link rel="alternate" type="application/feed+json" href="/jobs.json"></html>',
                "/jobs.json": '{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"role-1","url":"https://employer.example/jobs/1","title":"Python Engineer","content_text":"Python work. $90,000 per year.","location":"Remote - United States"}]}',
            }
            seen = []

            def fetch(client, url, deadline):
                parsed = urlsplit(url)
                seen.append(url)
                if parsed.path not in pages:
                    raise AssertionError(f"Unexpected synthetic fetch: {url}")
                request = httpx.Request("GET", url)
                return httpx.Response(200, text=pages[parsed.path], request=request)

            app = create_app(store, "http://127.0.0.1:1", root / "outputs",
                             redis_url="redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
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
                        form = page.get_by_role("heading", name="Add a company to look for jobs", exact=True).locator("..")
                        form.get_by_label("Company name").fill("Example Systems")
                        form.get_by_label("Company website (HTTPS)").fill("https://employer.example/")
                        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/companies/discovery")) as queued_response:
                            form.get_by_role("button", name="Discover company jobs", exact=True).click()
                        operation_id = queued_response.value.json()["operation_id"]
                        self.assertTrue(operation_id)

                        def run_worker():
                            worker_result["value"] = execute_operation(operation_id)

                        worker_thread = threading.Thread(target=run_worker, daemon=True)
                        worker_thread.start()
                        expect(page.get_by_role("status").filter(has_text="Completed")).to_be_visible(timeout=15000)
                        expect(page.get_by_role("status").filter(has_text="Public company jobs and source information have been updated.")).to_be_visible()
                        page.get_by_role("button", name=re.compile(r"^Jobs")).click()
                        card = page.locator(".job-card").filter(has_text="Example Systems").first
                        expect(card).to_be_visible()
                        expect(card).to_contain_text("Python Engineer")
                        expect(page.get_by_role("button", name="Sources", exact=True)).to_be_visible()
                        self.assertEqual(errors, [])
                        self.assertEqual(external, [])
                        context.close()
                        browser.close()
                    worker_thread.join(timeout=10)
                    self.assertFalse(worker_thread.is_alive(), "Public discovery worker did not finish")
                    self.assertEqual(worker_result.get("value", {}).get("status"), "COMPLETED", worker_result)
                self.assertEqual(set(seen), {"https://employer.example" + path for path in pages})
                with store.engine.connect() as connection:
                    source = connection.execute(select(m.company_sources)).mappings().one()
                    job = connection.execute(select(m.jobs)).mappings().one()
                    operation = connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one()
                self.assertFalse(source["enabled"])
                self.assertEqual(source["confidence"], 0.45)
                self.assertEqual(job["title"], "Python Engineer")
                self.assertEqual(operation["state"], "COMPLETED")
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated test API must stop")


if __name__ == "__main__":
    unittest.main()
