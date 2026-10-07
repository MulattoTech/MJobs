"""Connected browser coverage for applying a saved profile to the Jobs view."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
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
            for identifier, profile_id, name, title in (
                    ("python-search", "fixture", "Python remote", "Python Developer"),
                    ("data-search", "fixture", "Data remote", "Data Engineer"),
                    ("other-search", "other", "Other profile search", "Python Developer")):
                save_search_profile(store, {
                    "id": identifier, "profile_id": profile_id, "name": name,
                    "enabled": True, "include_titles": [title], "remote": "REQUIRED",
                    "employment_types": [], "source_ids": [],
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

                    search = page.get_by_label("Saved search profile", exact=True)
                    expect(search.locator("option", has_text="Python remote")).to_have_count(1)
                    expect(search.locator("option", has_text="Data remote")).to_have_count(1)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "search_profile_id=python-search" in response.url) as python_response:
                        search.select_option("python-search")
                    self.assertEqual([job["title"] for job in python_response.value.json()["jobs"]],
                                     ["Python Developer"])
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_have_count(0)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "search_profile_id=data-search" in response.url) as data_response:
                        search.select_option("data-search")
                    self.assertEqual([job["title"] for job in data_response.value.json()["jobs"]],
                                     ["Data Engineer"])
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_have_count(0)

                    search.select_option("")
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_be_visible()

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    isolated_search = page.get_by_label("Saved search profile", exact=True)
                    expect(isolated_search).to_have_value("")
                    expect(isolated_search.locator("option", has_text="Python remote")).to_have_count(0)
                    isolated_search.select_option("other-search")
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_have_count(0)

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
