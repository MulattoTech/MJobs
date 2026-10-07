"""Connected browser coverage for skill and location job search."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class JobFullTextSearchBrowserTests(unittest.TestCase):
    def test_jobs_ui_searches_skill_and_location_after_database_reopen(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-full-text-search-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")

            job = store.jobs(profile_id="fixture")[0]
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(
                    title="Platform Reliability Engineer",
                    description="Operate dependable internal services.",
                    skills=["Kubernetes", "PostgreSQL", "Terraform"],
                    location="Tulsa Oklahoma",
                ))
                connection.execute(update(m.companies).where(
                    m.companies.c.id == job["company_id"]).values(name="Example Platform Group"))
            store.engine.dispose()
            store = Store(database_url)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error"))
            thread = threading.Thread(target=server.run,
                                      kwargs={"sockets": [sock]}, daemon=True)
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated search API did not start")
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
                    search = page.get_by_label("Search jobs", exact=True)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "q=Kubernetes" in response.url) as skill_response:
                        search.fill("Kubernetes")
                    self.assertEqual([row["id"] for row in skill_response.value.json()["jobs"]],
                                     [job["id"]])
                    expect(page.locator(".job-card").filter(
                        has_text="Platform Reliability Engineer")).to_be_visible()

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "q=Tulsa" in response.url) as location_response:
                        search.fill("Tulsa")
                    self.assertEqual([row["id"] for row in location_response.value.json()["jobs"]],
                                     [job["id"]])
                    expect(page.locator(".job-card").filter(
                        has_text="Platform Reliability Engineer")).to_be_visible()

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "q=PostgreSQL" in response.url) as reopened_response:
                        page.get_by_label("Search jobs", exact=True).fill("PostgreSQL")
                    self.assertEqual([row["id"] for row in reopened_response.value.json()["jobs"]],
                                     [job["id"]])
                    expect(page.locator(".job-card").filter(
                        has_text="Platform Reliability Engineer")).to_be_visible()

                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated search API did not stop")


if __name__ == "__main__":
    unittest.main()
