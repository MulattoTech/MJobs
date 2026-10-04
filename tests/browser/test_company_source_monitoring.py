"""Real browser proof for reviewed public-source monitoring and refresh scheduling."""
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import re
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select, update
import uvicorn

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.search_profiles import save_search_profile
from services.discovery.scheduler import schedule_due_sources
from services.company_discovery.public_client import PublicCompanyClient
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class CompanySourceMonitoringBrowserTests(unittest.TestCase):
    def test_review_run_and_due_scheduler_refresh_real_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'monitoring.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            company_id = identity("public-company-origin", "https://employer.example")
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {"id": company_id, "name": "Example Public Employer", "domain": ""})
            source = upsert_company_job_source(
                store, company_id=company_id, provider="json_feed", tenant_identifier=company_id,
                career_url="https://employer.example/careers", feed_url="https://employer.example/jobs.json",
                collection_strategy="PUBLIC_JSON", confidence=0.45, enabled=True,
                discovered_automatically=True,
            )
            save_search_profile(store, {"id": "monitor-profile", "profile_id": "fixture", "name": "Monitor",
                                        "enabled": True, "include_titles": ["Engineer"], "remote": "ANY",
                                        "employment_types": [], "source_ids": []})
            feed_items = [{"id": "role-1", "url": "https://employer.example/jobs/1", "title": "Python Engineer",
                           "content_text": "Python work.", "location": "Remote - United States"}]
            pages = {
                "/robots.txt": "User-agent: *\nAllow: /",
                "/": '<html>Example Public Employer<a rel="careers" href="/careers">Careers</a></html>',
                "/careers": '<html>Example Public Employer<link rel="alternate" type="application/feed+json" href="/jobs.json"></html>',
                "/jobs/1": '<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","identifier":{"@type":"PropertyValue","value":"role-1"},"title":"Python Engineer","url":"https://employer.example/jobs/1"}</script>',
                "/jobs/2": '<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","identifier":{"@type":"PropertyValue","value":"role-2"},"title":"Platform Engineer","url":"https://employer.example/jobs/2"}</script>',
            }
            seen = []

            def fetch(client, url, deadline):
                parsed = urlsplit(url)
                seen.append(url)
                if parsed.path == "/jobs.json":
                    body = json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": feed_items})
                else:
                    if parsed.path not in pages:
                        raise AssertionError(f"Unexpected synthetic fetch: {url}")
                    body = pages[parsed.path]
                content_type = "application/json" if parsed.path.endswith(".json") else "text/html; charset=utf-8"
                return httpx.Response(200, text=body, headers={"content-type": content_type}, request=httpx.Request("GET", url))

            env = {"MJOBS_DATABASE_URL": store.url, "MJOBS_OPERATION_MODE": "PERSONAL",
                   "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0", "MJOBS_OUTPUT_DIR": str(root / "outputs")}
            app = create_app(store, "http://127.0.0.1:1", root / "outputs",
                             redis_url="redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
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
                        external, errors = [], []

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
                        source_card = page.locator(".company-source-card").first
                        expect(source_card).to_contain_text("Monitor this reviewed source")
                        with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/monitoring")):
                            source_card.get_by_role("button", name="Monitor this reviewed source", exact=True).click()
                        expect(source_card).to_contain_text("Idle")
                        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/refresh")) as refresh_response:
                            source_card.get_by_role("button", name="Run now", exact=True).click()
                        operation_id = refresh_response.value.json()["operation_id"]
                        worker_result = {}

                        def run_worker():
                            worker_result["value"] = execute_operation(operation_id)

                        worker = threading.Thread(target=run_worker, daemon=True)
                        worker.start()
                        expect(page.get_by_role("status").filter(has_text="Completed")).to_be_visible(timeout=15000)
                        expect(source_card).to_contain_text("Healthy")
                        expect(source_card).to_contain_text("Jobs Found")
                        expect(source_card).to_contain_text("1")
                        worker.join(timeout=10)
                        self.assertFalse(worker.is_alive(), "Initial monitoring worker did not finish")
                        self.assertEqual(worker_result.get("value", {}).get("status"), "COMPLETED", worker_result)

                        feed_items.append({"id": "role-2", "url": "https://employer.example/jobs/2", "title": "Platform Engineer",
                                           "content_text": "Platform work.", "location": "Remote - United States"})
                        due = datetime.now(timezone.utc) - timedelta(minutes=5)
                        with store.engine.begin() as connection:
                            connection.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.id == source["id"])
                                               .values(next_due=due.isoformat()))
                        scheduled = schedule_due_sources(store, datetime.now(timezone.utc))
                        self.assertEqual(len(scheduled["company_sources"]["scheduled"]), 1, scheduled)
                        scheduled_operation = scheduled["company_sources"]["scheduled"][0]["operation_id"]
                        second_result = execute_operation(scheduled_operation)
                        self.assertEqual(second_result["status"], "COMPLETED", second_result)
                        page.reload(wait_until="domcontentloaded")
                        page.locator("#active-profile").select_option("fixture")
                        page.get_by_role("button", name="Sources", exact=True).click()
                        source_card = page.locator(".company-source-card").first
                        expect(source_card).to_contain_text("Jobs Found")
                        expect(source_card).to_contain_text("2")
                        page.get_by_role("button", name=re.compile(r"^Jobs")).click()
                        expect(page.locator(".job-card").filter(has_text="Platform Engineer")).to_be_visible()
                        self.assertEqual(errors, [])
                        self.assertEqual(external, [])
                        context.close()
                        browser.close()
                    self.assertEqual(set(seen), {"https://employer.example" + path for path in pages if path != "/"} | {"https://employer.example/jobs.json"})
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated test API must stop")


if __name__ == "__main__":
    unittest.main()
