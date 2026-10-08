"""Connected FIXTURE browser acceptance for minimum match-score search filters."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select, update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.discovery.scheduler import schedule_due_sources
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class MatchScoreSearchFilterBrowserTests(unittest.TestCase):
    def test_score_cutoff_filters_saves_restores_clears_and_stays_profile_scoped(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-match-score-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                "email": "other@example.test"})
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")
            version = store.profile("fixture")["version"]
            jobs = {row["title"]: row for row in store.jobs()}
            with store.engine.begin() as connection:
                connection.execute(update(m.matches).where(
                    m.matches.c.profile_id == "fixture", m.matches.c.profile_version == version,
                    m.matches.c.job_id == jobs["Python Developer"]["id"]).values(score=75.0))
                connection.execute(update(m.matches).where(
                    m.matches.c.profile_id == "fixture", m.matches.c.profile_version == version,
                    m.matches.c.job_id == jobs["Data Engineer"]["id"]).values(score=40.0))
                match_rows_before = connection.execute(select(m.matches)).mappings().all()
            operations_before = store.count("worker_operations")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error"))
            try:
                thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated match-score API did not start")
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
                    score = page.get_by_label("Minimum match score (%)", exact=True)
                    with page.expect_response(lambda response: response.url.startswith(origin + "/jobs?")
                            and "min_match_score=75" in response.url) as cutoff:
                        score.fill("75")
                    self.assertEqual([job["title"] for job in cutoff.value.json()["jobs"]],
                                     ["Python Developer"])
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_have_count(0)
                    page.get_by_label("Saved search name", exact=True).fill("High score roles")
                    page.get_by_role("button", name="Save current filters", exact=True).click()
                    saved = page.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="High score roles")).to_have_count(1)

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    saved = page.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="High score roles")).to_have_count(1)
                    with page.expect_response(lambda response: response.url.startswith(origin + "/jobs?")
                            and "min_match_score=75" in response.url) as restored:
                        saved.select_option(saved.locator("option", has_text="High score roles").get_attribute("value"))
                    self.assertEqual([job["title"] for job in restored.value.json()["jobs"]],
                                     ["Python Developer"])
                    expect(page.get_by_label("Minimum match score (%)", exact=True)).to_have_value("75")
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_have_count(0)

                    with page.expect_response(lambda response: response.url.startswith(origin + "/jobs?")
                            and "min_match_score=" not in response.url) as cleared:
                        saved.select_option("")
                    self.assertEqual(len(cleared.value.json()["jobs"]), 2)
                    expect(page.get_by_label("Minimum match score (%)", exact=True)).to_have_value("")
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_be_visible()

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    isolated = page.get_by_label("Saved search profile", exact=True)
                    expect(isolated).to_have_value("")
                    expect(isolated.locator("option", has_text="High score roles")).to_have_count(0)
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
                with store.engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.matches)).mappings().all(), match_rows_before)
                self.assertEqual(store.count("worker_operations"), operations_before)
                self.assertEqual(schedule_due_sources(store)["scheduled"], [])
            finally:
                server.should_exit = True
                if 'thread' in locals():
                    thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated match-score API did not stop")


if __name__ == "__main__":
    unittest.main()
