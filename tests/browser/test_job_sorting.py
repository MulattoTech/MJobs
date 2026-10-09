"""Connected browser acceptance for job sorting and reviewed priority after reload."""
from datetime import datetime, timedelta, timezone
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
from services.discovery.search_profiles import save_search_profile
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
                    attributes={**older["attributes"], "reported_posted_at": "2026-10-01T00:00:00Z",
                                "salary_min": 90000, "salary_max": 110000,
                                "salary_currency": "USD", "salary_period": "YEAR"},
                ))
                connection.execute(update(m.jobs).where(m.jobs.c.id == newer["id"]).values(
                    title="Zulu Newest Role",
                    attributes={**newer["attributes"], "reported_posted_at": "2026-10-07T00:00:00Z",
                                "salary_min": 120000, "salary_max": 130000,
                                "salary_currency": "USD", "salary_period": "YEAR"},
                ))
            save_search_profile(store, {
                "id": "salary-saved", "profile_id": "fixture", "name": "Salary descending",
                "enabled": False, "include_titles": [], "remote": "ANY", "employment_types": [],
                "source_ids": [], "job_filters": {"q": "", "title": "", "company": "",
                                                     "location": "", "salary_min": "", "salary_max": "",
                                                     "remote_type": "", "provider": "", "sort": "salary_high"},
            })
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

                    expect(page.get_by_text("Salary sorting compares explicit annual USD only.", exact=False)).to_be_visible()
                    page.get_by_label("Saved search profile", exact=True).select_option("salary-saved")
                    expect(sort).to_have_value("salary_high")
                    expect(page.locator(".job-card h2").first).to_have_text("Zulu Newest Role")
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "sort=salary_low" in response.url) as salary_response:
                        sort.select_option("salary_low")
                    self.assertEqual([row["title"] for row in salary_response.value.json()["jobs"]],
                                     ["Alpha Older Role", "Zulu Newest Role"])

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

                    # Build two explicit, conflicting ranking signals. The newer
                    # job has a stronger skill match but salary mismatch; reviewed
                    # salary feedback should move the salary-aligned job upward.
                    with store.engine.begin() as connection:
                        comparable_posted_at = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
                        for job, score, salary_status in (
                            (older, 40.0, "ALIGNS"), (newer, 95.0, "NO_DIRECT_MATCH"),
                        ):
                            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(
                                attributes={**job["attributes"], "reported_posted_at": comparable_posted_at},
                            ))
                            connection.execute(update(m.matches).where(
                                m.matches.c.job_id == job["id"],
                                m.matches.c.profile_id == "fixture",
                            ).values(score=score, components={"fit_factors": [{
                                "id": "salary", "label": "Salary expectation",
                                "status": salary_status, "explanation": "Explicit synthetic salary comparison.",
                                "score_effect": "NONE",
                            }]}))

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "sort=application_priority" in response.url) as initial_priority:
                        sort.select_option("application_priority")
                    initial_ranked = initial_priority.value.json()["jobs"]
                    self.assertEqual([row["title"] for row in initial_ranked],
                                     ["Zulu Newest Role", "Alpha Older Role"])

                    feedback = page.request.post(
                        f"{origin}/profiles/fixture/jobs/{older['id']}/match-feedback",
                        data={"category": "BAD_SALARY", "request_id": "salary-priority-feedback"},
                    )
                    self.assertEqual(feedback.status, 200, feedback.text())
                    applied = page.request.post(
                        f"{origin}/profiles/fixture/match-preferences",
                        data={"feedback_id": feedback.json()["feedback"]["id"],
                              "dimension": "salary", "direction": "increase",
                              "request_id": "salary-priority-apply"},
                    )
                    self.assertEqual(applied.status, 200, applied.text())

                    sort.select_option("newest")
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "sort=application_priority" in response.url) as reviewed_priority:
                        sort.select_option("application_priority")
                    reviewed_ranked = reviewed_priority.value.json()["jobs"]
                    self.assertEqual([row["title"] for row in reviewed_ranked],
                                     ["Alpha Older Role", "Zulu Newest Role"],
                                     [(row["title"], row["application_priority_score"],
                                       row["application_priority_band"]) for row in reviewed_ranked])
                    self.assertGreater(reviewed_ranked[0]["application_priority_score"],
                                       reviewed_ranked[1]["application_priority_score"])
                    expect(page.locator(".job-card").first).to_contain_text("Application priority")
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
