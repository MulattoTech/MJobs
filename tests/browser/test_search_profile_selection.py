"""Connected browser coverage for applying a saved profile to the Jobs view."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select, update
import uvicorn

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
class SearchProfileSelectionBrowserTests(unittest.TestCase):
    def test_saved_profiles_filter_jobs_and_stay_isolated_by_career_profile(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-search-profile-browser-") as directory:
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
                discover(store, source_origin, "other")
            with store.engine.begin() as connection:
                legacy = connection.execute(select(m.jobs).where(
                    m.jobs.c.title == "Python Developer",
                )).mappings().one()
                connection.execute(update(m.jobs).where(m.jobs.c.id == legacy["id"]).values(
                    location="Remote, United States", remote_type="HYBRID",
                    description="Maintain our hybrid cloud infrastructure.",
                    attributes={**legacy["attributes"], "provider": "greenhouse", "remote_indicated": True},
                ))
            for identifier, profile_id, name, title in (
                    ("python-search", "fixture", "Python remote", "Python Developer"),
                    ("data-search", "fixture", "Data remote", "Data Engineer"),
                    ("other-search", "other", "Other profile search", "Python Developer")):
                save_search_profile(store, {
                    "id": identifier, "profile_id": profile_id, "name": name,
                    "enabled": True, "include_titles": [title], "remote": "REQUIRED",
                    "employment_types": [], "source_ids": [],
                    **({"remote_residence_state": "OK"} if identifier == "python-search" else {}),
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
                        self.fail("Isolated saved-search API did not start")
                    time.sleep(0.02)

                external = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        elif (parsed.path == "/jobs"
                              and parse_qs(parsed.query).get("search_profile_id") == ["python-search"]):
                            # Exercise the frontend contract even when the backend has yet to emit the additive field.
                            response = route.fetch()
                            body = response.json()
                            for row in body["jobs"]:
                                row["search_evidence"].setdefault("work_arrangement", "REMOTE")
                            route.fulfill(response=response, json=body)
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")

                    # The onboarding guide exposes its own earlier saved-search selector.
                    search = page.get_by_label("Saved search profile", exact=True).last
                    expect(search.locator("option", has_text="Python remote")).to_have_count(1)
                    expect(search.locator("option", has_text="Data remote")).to_have_count(1)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "search_profile_id=python-search" in response.url) as python_response:
                        search.select_option("python-search")
                    self.assertEqual([job["title"] for job in python_response.value.json()["jobs"]],
                                     ["Python Developer"])
                    python_card = page.locator(".job-card").filter(has_text="Python Developer")
                    detail = page.locator(".job-detail")
                    expect(python_card).to_contain_text("Remote, United States · REMOTE")
                    expect(python_card).to_contain_text("Remote eligibility confirmed for OK")
                    expect(python_card).not_to_contain_text("HYBRID")
                    python_card.click()
                    expect(detail).to_contain_text("Remote, United States · REMOTE")
                    expect(detail.get_by_label("Remote location eligibility")).to_contain_text(
                        "Remote eligibility confirmed for OK",
                    )
                    expect(detail).not_to_contain_text("Remote, United States · HYBRID")
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_have_count(0)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "search_profile_id=data-search" in response.url) as data_response:
                        search.select_option("data-search")
                    self.assertEqual([job["title"] for job in data_response.value.json()["jobs"]],
                                     ["Data Engineer"])
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_have_count(0)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "search_profile_id=data-search" in response.url) as restored_data_response:
                        page.reload(wait_until="domcontentloaded")
                    self.assertEqual([job["title"] for job in restored_data_response.value.json()["jobs"]],
                                     ["Data Engineer"])
                    expect(page.get_by_label("Saved search profile", exact=True).last).to_have_value(
                        "data-search")

                    search.select_option("")
                    expect(search).to_have_value("")
                    page.reload(wait_until="domcontentloaded")
                    search = page.get_by_label("Saved search profile", exact=True).last
                    expect(search).to_have_value("")
                    expect(python_card).to_contain_text("Remote, United States · HYBRID")
                    expect(python_card).not_to_contain_text("Remote eligibility confirmed")
                    python_card.click()
                    expect(detail).to_contain_text("Remote, United States · HYBRID")
                    expect(detail.get_by_label("Remote location eligibility")).to_have_count(0)
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_be_visible()

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    isolated_search = page.get_by_label("Saved search profile", exact=True).last
                    expect(isolated_search).to_have_value("")
                    expect(isolated_search.locator("option", has_text="Python remote")).to_have_count(0)
                    isolated_search.select_option("other-search")
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_have_count(0)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "search_profile_id=other-search" in response.url) as other_restored:
                        page.reload(wait_until="domcontentloaded")
                    self.assertEqual([job["title"] for job in other_restored.value.json()["jobs"]],
                                     ["Python Developer"])
                    expect(page.get_by_label("Saved search profile", exact=True).last).to_have_value(
                        "other-search")

                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    fixture_search = page.get_by_label("Saved search profile", exact=True).last
                    expect(fixture_search).to_have_value("")

                    page.goto(origin + "/?salary_min=25&salary_period=HOUR", wait_until="domcontentloaded")
                    fixture_search = page.get_by_label("Saved search profile", exact=True).last
                    expect(fixture_search.locator("option", has_text="Python remote")).to_have_count(1)
                    expect(fixture_search).to_have_value("")
                    expect(page.get_by_label("Salary unit", exact=True)).to_have_value("HOUR")
                    hourly_minimum = page.get_by_label("Minimum hourly salary (USD)", exact=True)
                    expect(hourly_minimum).to_have_value("25")
                    self.assertEqual(parse_qs(urlsplit(page.url).query).get("salary_min"), ["25"])

                    fixture_search.select_option("python-search")
                    page.get_by_label("Salary unit", exact=True).select_option("HOUR")
                    hourly_minimum.fill("25")
                    expect(hourly_minimum).to_have_value("25")
                    fixture_search.select_option("")
                    expect(page.get_by_label("Salary unit", exact=True)).to_have_value("YEAR")
                    expect(page.get_by_label("Minimum annual salary (USD)", exact=True)).to_have_value("")
                    expect(page).to_have_url(origin + "/")

                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated saved-search API did not stop")


if __name__ == "__main__":
    unittest.main()
