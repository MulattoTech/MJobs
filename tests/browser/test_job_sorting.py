"""Connected browser acceptance for Newest sorting after profile reload."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import insert, update

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
class JobSortingBrowserTests(unittest.TestCase):
    def test_newest_selection_orders_connected_jobs_after_profile_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-newest-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")
            jobs = sorted(store.jobs(profile_id="fixture"), key=lambda row: row["id"])
            older, newer = jobs
            with store.engine.begin() as connection:
                for job, company_name in ((older, "alpha Systems"), (newer, "Zulu Systems")):
                    company_id = f"sort-company-{job['id']}"
                    connection.execute(insert(m.companies).values(
                        id=company_id, name=company_name, domain="",
                        parent_company_id=None, relationship_type=None,
                    ))
                    connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"])
                                       .values(company_id=company_id))
                connection.execute(update(m.jobs).where(m.jobs.c.id == older["id"]).values(
                    title="Alpha Older Role",
                    attributes={**older["attributes"], "reported_posted_at": "2026-10-01T00:00:00Z"},
                ))
                connection.execute(update(m.jobs).where(m.jobs.c.id == newer["id"]).values(
                    title="Zulu Newest Role",
                    attributes={**newer["attributes"], "reported_posted_at": "2026-10-07T00:00:00Z"},
                ))
            store.engine.dispose()
            store = Store(database_url)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated sorting API did not start")
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
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    sort = page.get_by_label("Sort", exact=True)
                    expect(page.locator(".job-card h2").first).to_have_text("Alpha Older Role")

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "sort=newest" in response.url) as sorted_response:
                        sort.select_option("newest")
                    self.assertEqual([row["title"] for row in sorted_response.value.json()["jobs"]],
                                     ["Zulu Newest Role", "Alpha Older Role"])
                    expect(page.locator(".job-card h2").first).to_have_text("Zulu Newest Role")
                    expect(page.locator(".job-card h2").nth(1)).to_have_text("Alpha Older Role")

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "sort=company" in response.url) as company_response:
                        sort.select_option("company")
                    self.assertEqual([row["title"] for row in company_response.value.json()["jobs"]],
                                     ["Alpha Older Role", "Zulu Newest Role"])
                    expect(page.locator(".job-card h2").first).to_have_text("Alpha Older Role")
                    expect(page.locator(".job-card h2").nth(1)).to_have_text("Zulu Newest Role")
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated sorting API did not stop")


if __name__ == "__main__":
    unittest.main()
