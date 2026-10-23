"""Connected FIXTURE browser acceptance for saved Jobs filters."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select, update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.sources import register_ats_tenant
from services.discovery.pipeline import discover
from services.discovery.scheduler import schedule_due_sources
from services.discovery.sources.collect import configure_source
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
            source_a = configure_source(store, "saved-filter-source-a")
            source_b = configure_source(store, "saved-filter-source-b")
            ats_routes = [
                ("Python Developer", "python-board", "python-board", 0.95, ["QUALIFIED_GREENHOUSE_ROUTE"]),
                ("Data Engineer", "source-only-board", None, 0.95, ["TEST_EVIDENCE"]),
                ("Data Engineer", "route-mismatch", "different-tenant", 0.95, ["TEST_EVIDENCE"]),
                ("Data Engineer", "low-confidence-board", "low-confidence-board", 0.74, ["TEST_EVIDENCE"]),
                ("Data Engineer", "blank-evidence-board", "blank-evidence-board", 0.95, ["  "]),
                ("Data Engineer", "ambiguous-board", "ambiguous-board", 0.95, ["TEST_EVIDENCE"]),
            ]
            for title, route_tenant, identity_tenant, confidence, evidence in ats_routes:
                with store.engine.connect() as connection:
                    job = connection.execute(select(m.jobs).where(m.jobs.c.title == title)).mappings().one()
                if identity_tenant is not None:
                    register_ats_tenant(store, job["company_id"], provider="greenhouse",
                                        tenant_identifier=identity_tenant,
                                        confidence=confidence, evidence=evidence)
                with store.engine.begin() as connection:
                    upsert(connection, m.routes, {
                        "id": identity("browser-ats-route", title, route_tenant),
                        "job_id": job["id"], "company_id": job["company_id"],
                        "provider": "greenhouse", "tenant": route_tenant,
                        "requisition": "browser-ats-filter-" + route_tenant,
                        "url": f"https://jobs.example.test/greenhouse/{route_tenant}/{title}",
                    })
            with store.engine.connect() as connection:
                ambiguous_job = connection.execute(select(m.jobs).where(
                    m.jobs.c.title == "Data Engineer")).mappings().one()
            register_ats_tenant(store, ambiguous_job["company_id"], provider="greenhouse",
                                tenant_identifier="ambiguous-board",
                                organization_identifier="second-organization",
                                confidence=0.95, evidence=["SECOND_TEST_EVIDENCE"])
            fixture_now = datetime.now(timezone.utc)
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer")
                                   .values(location="Boston, MA", attributes={
                                       "salary_min": 80000, "salary_max": 100000,
                                       "salary_currency": "USD", "salary_period": "YEAR",
                                       "experience_level": "SENIOR", "employment_type": "FULL_TIME",
                                       "skill_ids": ["PYTHON"],
                                       "education_requirement": "BACHELORS_REQUIRED",
                                       "clearance": {"level": "PUBLIC_TRUST"},
                                       "source_id": source_a,
                                       "reported_posted_at": (fixture_now - timedelta(days=1)).isoformat()}))
                connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer")
                                   .values(location="Berlin", attributes={
                                       "salary_min": 150000, "salary_max": 180000,
                                       "salary_currency": "EUR", "salary_period": "YEAR",
                                       "experience_level": "STAFF_PLUS", "employment_type": "OTHER",
                                       "skill_ids": ["SQL"],
                                       "education_requirement": "ASSOCIATE_REQUIRED",
                                       "clearance": {"level": "SECRET"},
                                       "source_id": source_b,
                                       "reported_posted_at": (fixture_now - timedelta(days=60)).isoformat()}))
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
                            parts = urlsplit(route.request.url)
                            query = dict(parse_qsl(parts.query))
                            if parts.path == "/jobs" and query.get("search_profile_id") == "limited-semantic":
                                # Exercise the real selected-search UI against a one-row API page.
                                query["limit"] = "1"
                                route.continue_(url=urlunsplit(parts._replace(query=urlencode(query))))
                            else:
                                route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    filters = page.locator(".filters")
                    created = context.request.put(origin + "/search-profiles", data={
                        "id": "limited-semantic", "profile_id": "fixture", "name": "Semantic Python only",
                        "enabled": False, "include_titles": ["Python Developer"], "remote": "ANY",
                        "employment_types": [], "source_ids": [],
                        "job_filters": {"q": "", "remote_type": "", "provider": "", "sort": "title"},
                    })
                    self.assertEqual(created.status, 200, created.text())
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    selected_search = filters.get_by_label("Saved search profile", exact=True)
                    with page.expect_response(lambda response:
                            "/jobs?" in response.url and "search_profile_id=limited-semantic" in response.url) as limited:
                        selected_search.select_option("limited-semantic")
                    self.assertEqual([job["title"] for job in limited.value.json()["jobs"]], ["Python Developer"])
                    expect(page.get_by_role("heading", name="Python Developer", exact=True)).to_be_visible()
                    selected_search.select_option("")
                    expect(page.get_by_label("Education requirement", exact=True)).to_have_value("")
                    expect(page.get_by_label("Education requirement", exact=True).locator("option")).to_have_count(6)
                    expect(page.get_by_label("Skill", exact=True)).to_have_value("")
                    expect(page.get_by_label("Skill", exact=True).locator("option", has_text="Active Directory")).to_have_count(1)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and f"job_source_id={source_a}" in response.url) as source_only:
                        page.get_by_label("Job source", exact=True).select_option(source_a)
                    self.assertEqual(source_only.value.status, 200, source_only.value.text())
                    self.assertEqual([job["title"] for job in source_only.value.json()["jobs"]],
                                     ["Python Developer"])
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "job_source_id=" not in response.url) as source_cleared:
                        page.get_by_label("Job source", exact=True).select_option("")
                    self.assertEqual(len(source_cleared.value.json()["jobs"]), 2)

                    # Canonical date-only changes fetch immediately; custom bounds remain explicit UTC.
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "date_window=LAST_HOUR" in response.url) as last_hour:
                        page.get_by_label("Posted within", exact=True).select_option("LAST_HOUR")
                    self.assertEqual(last_hour.value.status, 200)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "date_window=TODAY" in response.url) as today:
                        page.get_by_label("Posted within", exact=True).select_option("TODAY")
                    self.assertEqual(today.value.status, 200)
                    page.get_by_label("Posted within", exact=True).select_option("CUSTOM")
                    expect(page.get_by_label("Posted after (UTC)", exact=True)).to_be_visible()
                    expect(page.get_by_label("Posted before (UTC)", exact=True)).to_be_visible()
                    page.get_by_label("Posted after (UTC)", exact=True).fill("2026-10-01T00:00:00")
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "date_window=CUSTOM" in response.url
                            and "date_start=2026-10-01T00%3A00%3A00Z" in response.url
                            and "date_end=2026-10-02T00%3A00%3A00Z" in response.url) as custom:
                        page.get_by_label("Posted before (UTC)", exact=True).fill("2026-10-02T00:00:00")
                    self.assertEqual(custom.value.status, 200)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "date_window" not in response.url
                            and "date_start" not in response.url
                            and "date_end" not in response.url
                            and "posted_within_days" not in response.url) as date_cleared:
                        page.get_by_label("Posted within", exact=True).select_option("")
                    self.assertEqual(date_cleared.value.status, 200)
                    expect(page.get_by_label("Posted after (UTC)", exact=True)).to_have_count(0)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "skill=PYTHON" in response.url) as skill_only:
                        page.get_by_label("Skill", exact=True).select_option("PYTHON")
                    self.assertEqual([job["title"] for job in skill_only.value.json()["jobs"]], ["Python Developer"])
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?") and "skill=" not in response.url) as skill_cleared:
                        page.get_by_label("Skill", exact=True).select_option("")
                    self.assertEqual(skill_cleared.value.status, 200)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "ats_provider=greenhouse" in response.url) as greenhouse_jobs:
                        page.get_by_label("ATS", exact=True).select_option("greenhouse")
                    self.assertEqual([job["title"] for job in greenhouse_jobs.value.json()["jobs"]],
                                     ["Python Developer"])
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "ats_provider=mock-ats" in response.url) as mock_jobs:
                        page.get_by_label("ATS", exact=True).select_option("mock-ats")
                    self.assertEqual(len(mock_jobs.value.json()["jobs"]), 2)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?") and "ats_provider=" not in response.url) as ats_cleared:
                        page.get_by_label("ATS", exact=True).select_option("")
                    self.assertEqual(len(ats_cleared.value.json()["jobs"]), 2)

                    page.get_by_label("Search jobs", exact=True).fill("Developer")
                    page.get_by_label("Job title", exact=True).fill("Python")
                    page.get_by_label("Company", exact=True).fill("Example Systems")
                    page.get_by_label("Location text", exact=True).fill("bOsToN")
                    page.get_by_label("Minimum annual salary (USD)", exact=True).fill("80000")
                    page.get_by_label("Maximum annual salary (USD)", exact=True).fill("100000")
                    page.get_by_label("Experience level", exact=True).select_option("SENIOR")
                    page.get_by_label("Employment type", exact=True).select_option("FULL_TIME")
                    page.get_by_label("Skill", exact=True).select_option("PYTHON")
                    page.get_by_label("Education requirement", exact=True).select_option("BACHELORS_REQUIRED")
                    page.get_by_label("Clearance requirement", exact=True).select_option("PUBLIC_TRUST")
                    page.get_by_label("Posted within", exact=True).select_option("LAST_7_DAYS")
                    page.get_by_label("Work location", exact=True).select_option("REMOTE")
                    page.get_by_label("Application provider", exact=True).select_option("mock-ats")
                    page.get_by_label("Job source", exact=True).select_option(source_a)
                    page.get_by_label("ATS", exact=True).select_option("greenhouse")
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "experience_level=SENIOR" in response.url
                            and "employment_type=FULL_TIME" in response.url
                            and "skill=PYTHON" in response.url
                            and "education_requirement=BACHELORS_REQUIRED" in response.url
                            and "clearance_level=PUBLIC_TRUST" in response.url
                            and "ats_provider=greenhouse" in response.url
                            and f"job_source_id={source_a}" in response.url
                            and "salary_min=80000" in response.url
                            and "salary_max=100000" in response.url
                            and "sort=company" in response.url) as sorted_jobs:
                        page.get_by_label("Sort", exact=True).select_option("company")
                    self.assertEqual([job["title"] for job in sorted_jobs.value.json()["jobs"]],
                                     ["Python Developer"])
                    page.get_by_label("Saved search name", exact=True).fill("Remote Python roles")
                    page.get_by_role("button", name="Save current filters", exact=True).click()
                    saved = filters.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Remote Python roles")).to_have_count(1)

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    saved = filters.get_by_label("Saved search profile", exact=True)
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
                            and "education_requirement=BACHELORS_REQUIRED" in response.url
                            and "clearance_level=PUBLIC_TRUST" in response.url
                            and "ats_provider=greenhouse" in response.url
                            and f"job_source_id={source_a}" in response.url
                            and "salary_min=80000" in response.url
                            and "salary_max=100000" in response.url
                            and "date_window=LAST_7_DAYS" in response.url
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
                    expect(page.get_by_label("Education requirement", exact=True)).to_have_value("BACHELORS_REQUIRED")
                    expect(page.get_by_label("Clearance requirement", exact=True)).to_have_value("PUBLIC_TRUST")
                    expect(page.get_by_label("ATS", exact=True)).to_have_value("greenhouse")
                    expect(page.get_by_label("Posted within", exact=True)).to_have_value("LAST_7_DAYS")
                    expect(page.get_by_label("Work location", exact=True)).to_have_value("REMOTE")
                    expect(page.get_by_label("Application provider", exact=True)).to_have_value("mock-ats")
                    expect(page.get_by_label("Job source", exact=True)).to_have_value(source_a)
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
                            and "education_requirement=" not in response.url) as education_cleared:
                        page.get_by_label("Education requirement", exact=True).select_option("")
                    self.assertEqual(education_cleared.value.status, 200)
                    expect(page.get_by_label("Education requirement", exact=True)).to_have_value("")

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "clearance_level=" not in response.url) as clearance_cleared:
                        page.get_by_label("Clearance requirement", exact=True).select_option("")
                    self.assertEqual(clearance_cleared.value.status, 200)
                    expect(page.get_by_label("Clearance requirement", exact=True)).to_have_value("")

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "q=" in response.url
                            and "title=" not in response.url
                            and "company=" not in response.url
                            and "location=" not in response.url
                            and "experience_level=" not in response.url
                            and "employment_type=" not in response.url
                            and "skill=" not in response.url
                            and "education_requirement=" not in response.url
                            and "clearance_level=" not in response.url
                            and "ats_provider=" not in response.url
                            and "job_source_id=" not in response.url
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
                    expect(page.get_by_label("Education requirement", exact=True)).to_have_value("")
                    expect(page.get_by_label("Clearance requirement", exact=True)).to_have_value("")
                    expect(page.get_by_label("ATS", exact=True)).to_have_value("")
                    expect(page.get_by_label("Posted within", exact=True)).to_have_value("")
                    expect(page.get_by_label("Work location", exact=True)).to_have_value("")
                    expect(page.get_by_label("Application provider", exact=True)).to_have_value("")
                    expect(page.get_by_label("Job source", exact=True)).to_have_value("")
                    expect(page.get_by_label("Sort", exact=True)).to_have_value("score")

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    isolated_search = filters.get_by_label("Saved search profile", exact=True)
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
