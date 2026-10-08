"""Connected FIXTURE browser acceptance for saved Jobs filters."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update
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
class SavedSearchFiltersBrowserTests(unittest.TestCase):
    def test_saved_filters_restore_controls_results_and_clear_with_profile_isolation(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-saved-filters-browser-") as directory:
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
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer")
                                   .values(location="Boston, MA", attributes={
                                       "salary_min": 80000, "salary_max": 100000,
                                       "salary_currency": "USD", "salary_period": "YEAR",
                                       "experience_level": "SENIOR", "employment_type": "FULL_TIME",
                                       "skill_ids": ["PYTHON"],
                                       "reported_posted_at": "2026-10-07T00:00:00+00:00"}))
                connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer")
                                   .values(location="Berlin", attributes={
                                       "salary_min": 150000, "salary_max": 180000,
                                       "salary_currency": "EUR", "salary_period": "YEAR",
                                       "experience_level": "STAFF_PLUS", "employment_type": "OTHER",
                                       "skill_ids": ["SQL"],
                                       "reported_posted_at": "2026-09-01T00:00:00+00:00"}))
                connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer")
                                   .values(skills=["Python"]))
                connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer")
                                   .values(skills=["SQL"]))
            operations_before = store.count("worker_operations")
            store.engine.dispose()
            store = Store(database_url)

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
                        self.fail("Isolated saved-search API did not start")
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
                    expect(page.get_by_label("Skill", exact=True)).to_have_value("")
                    expect(page.get_by_label("Skill", exact=True).locator("option", has_text="Active Directory")).to_have_count(1)

                    # A date-only change must fetch new results immediately, then clear cleanly.
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "posted_within_days=7" in response.url) as date_only:
                        page.get_by_label("Posted within", exact=True).select_option("7")
                    self.assertEqual(date_only.value.status, 200)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "posted_within_days" not in response.url) as date_cleared:
                        page.get_by_label("Posted within", exact=True).select_option("")
                    self.assertEqual(date_cleared.value.status, 200)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "skill=PYTHON" in response.url) as skill_only:
                        page.get_by_label("Skill", exact=True).select_option("PYTHON")
                    self.assertEqual([job["title"] for job in skill_only.value.json()["jobs"]], ["Python Developer"])
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?") and "skill=" not in response.url) as skill_cleared:
                        page.get_by_label("Skill", exact=True).select_option("")
                    self.assertEqual(skill_cleared.value.status, 200)

                    page.get_by_label("Search jobs", exact=True).fill("Developer")
                    page.get_by_label("Job title", exact=True).fill("Python")
                    page.get_by_label("Company", exact=True).fill("Example Systems")
                    page.get_by_label("Location text", exact=True).fill("bOsToN")
                    page.get_by_label("Minimum annual salary (USD)", exact=True).fill("80000")
                    page.get_by_label("Maximum annual salary (USD)", exact=True).fill("100000")
                    page.get_by_label("Experience level", exact=True).select_option("SENIOR")
                    page.get_by_label("Employment type", exact=True).select_option("FULL_TIME")
                    page.get_by_label("Skill", exact=True).select_option("PYTHON")
                    page.get_by_label("Posted within", exact=True).select_option("7")
                    page.get_by_label("Work location", exact=True).select_option("REMOTE")
                    page.get_by_label("Source", exact=True).select_option("mock-ats")
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "experience_level=SENIOR" in response.url
                            and "employment_type=FULL_TIME" in response.url
                            and "skill=PYTHON" in response.url
                            and "salary_min=80000" in response.url
                            and "salary_max=100000" in response.url
                            and "sort=company" in response.url) as sorted_jobs:
                        page.get_by_label("Sort", exact=True).select_option("company")
                    self.assertEqual([job["title"] for job in sorted_jobs.value.json()["jobs"]],
                                     ["Python Developer"])
                    page.get_by_label("Saved search name", exact=True).fill("Remote Python roles")
                    page.get_by_role("button", name="Save current filters", exact=True).click()
                    saved = page.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Remote Python roles")).to_have_count(1)

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    saved = page.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Remote Python roles")).to_have_count(1)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "q=Developer" in response.url
                            and "title=Python" in response.url
                            and "company=Example+Systems" in response.url
                            and "location=bOsToN" in response.url
                            and "experience_level=SENIOR" in response.url
                            and "employment_type=FULL_TIME" in response.url
                            and "skill=PYTHON" in response.url
                            and "salary_min=80000" in response.url
                            and "salary_max=100000" in response.url
                            and "remote_type=REMOTE" in response.url
                            and "provider=mock-ats" in response.url
                            and "sort=company" in response.url) as restored_jobs:
                        saved.select_option(saved.locator("option", has_text="Remote Python roles").get_attribute("value"))
                    self.assertEqual([job["title"] for job in restored_jobs.value.json()["jobs"]],
                                     ["Python Developer"])
                    expect(page.get_by_label("Search jobs", exact=True)).to_have_value("Developer")
                    expect(page.get_by_label("Job title", exact=True)).to_have_value("Python")
                    expect(page.get_by_label("Company", exact=True)).to_have_value("Example Systems")
                    expect(page.get_by_label("Location text", exact=True)).to_have_value("bOsToN")
                    expect(page.get_by_label("Minimum annual salary (USD)", exact=True)).to_have_value("80000")
                    expect(page.get_by_label("Maximum annual salary (USD)", exact=True)).to_have_value("100000")
                    expect(page.get_by_label("Experience level", exact=True)).to_have_value("SENIOR")
                    expect(page.get_by_label("Employment type", exact=True)).to_have_value("FULL_TIME")
                    expect(page.get_by_label("Skill", exact=True)).to_have_value("PYTHON")
                    expect(page.get_by_label("Posted within", exact=True)).to_have_value("7")
                    expect(page.get_by_label("Work location", exact=True)).to_have_value("REMOTE")
                    expect(page.get_by_label("Source", exact=True)).to_have_value("mock-ats")
                    expect(page.get_by_label("Sort", exact=True)).to_have_value("company")
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_have_count(0)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "employment_type=" not in response.url) as employment_cleared:
                        page.get_by_label("Employment type", exact=True).select_option("")
                    self.assertEqual(employment_cleared.value.status, 200)
                    expect(page.get_by_label("Employment type", exact=True)).to_have_value("")

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "skill=" not in response.url) as saved_skill_cleared:
                        page.get_by_label("Skill", exact=True).select_option("")
                    self.assertEqual(saved_skill_cleared.value.status, 200)
                    expect(page.get_by_label("Skill", exact=True)).to_have_value("")

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "q=" in response.url
                            and "title=" not in response.url
                            and "company=" not in response.url
                            and "location=" not in response.url
                            and "experience_level=" not in response.url
                            and "employment_type=" not in response.url
                            and "skill=" not in response.url
                            and "salary_min=" not in response.url
                            and "salary_max=" not in response.url
                            and "remote_type=" not in response.url
                            and "provider=" not in response.url
                            and "sort=score" in response.url) as cleared_jobs:
                        saved.select_option("")
                    self.assertEqual(len(cleared_jobs.value.json()["jobs"]), 2)
                    expect(page.get_by_label("Search jobs", exact=True)).to_have_value("")
                    expect(page.get_by_label("Job title", exact=True)).to_have_value("")
                    expect(page.get_by_label("Company", exact=True)).to_have_value("")
                    expect(page.get_by_label("Location text", exact=True)).to_have_value("")
                    expect(page.get_by_label("Minimum annual salary (USD)", exact=True)).to_have_value("")
                    expect(page.get_by_label("Maximum annual salary (USD)", exact=True)).to_have_value("")
                    expect(page.get_by_label("Experience level", exact=True)).to_have_value("")
                    expect(page.get_by_label("Employment type", exact=True)).to_have_value("")
                    expect(page.get_by_label("Skill", exact=True)).to_have_value("")
                    expect(page.get_by_label("Posted within", exact=True)).to_have_value("")
                    expect(page.get_by_label("Work location", exact=True)).to_have_value("")
                    expect(page.get_by_label("Source", exact=True)).to_have_value("")
                    expect(page.get_by_label("Sort", exact=True)).to_have_value("score")

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    isolated_search = page.get_by_label("Saved search profile", exact=True)
                    expect(isolated_search).to_have_value("")
                    expect(isolated_search.locator("option", has_text="Remote Python roles")).to_have_count(0)
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
                self.assertEqual(store.count("worker_operations"), operations_before)
                self.assertEqual(schedule_due_sources(store)["scheduled"], [])
            finally:
                server.should_exit = True
                if 'thread' in locals():
                    thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated saved-search API did not stop")


if __name__ == "__main__":
    unittest.main()
