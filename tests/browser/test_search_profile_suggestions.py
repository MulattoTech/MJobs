"""Connected FIXTURE browser acceptance for search-profile title suggestions."""
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.pipeline import discover
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SearchProfileSuggestionsBrowserTests(unittest.TestCase):
    def test_current_strong_titles_are_explained_and_added_only_on_explicit_click(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-search-profile-suggestions-browser-") as directory:
            root = Path(directory)
            database = root / "browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                "email": "other@example.test"})
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")

            seed = {
                "fixture-search": ("fixture", "Alex search", ["System Administrator"]),
                "other-search": ("other", "Other search", ["Network Administrator"]),
            }
            for identifier, (profile_id, name, titles) in seed.items():
                save_search_profile(store, {
                    "id": identifier, "profile_id": profile_id, "name": name,
                    "enabled": True, "include_titles": titles, "remote": "REQUIRED",
                    "employment_types": [], "source_ids": [],
                })

            fixture_version = store.profile("fixture")["version"]
            other_version = store.profile("other")["version"]
            synthetic_titles = (
                ("Infrastructure Support Engineer", "fixture", fixture_version, 92),
                ("IT Operations Engineer", "fixture", fixture_version, 82),
                ("Junior Helpdesk Technician", "fixture", fixture_version, 74),
                ("Legacy Systems Engineer", "fixture", "stale-profile-version", 99),
                ("System Administrator", "fixture", fixture_version, 95),
                ("Platform Administrator", "other", other_version, 90),
            )
            with store.engine.begin() as connection:
                template = dict(connection.execute(select(m.jobs).limit(1)).mappings().one())
                for title, profile_id, profile_version, score in synthetic_titles:
                    job_id = identity("browser-search-profile-suggestion", title)
                    connection.execute(m.jobs.insert().values(**{
                        **template, "id": job_id, "title": title,
                        "requisition": "suggestion-" + job_id[:20],
                    }))
                    connection.execute(m.matches.insert().values(
                        job_id=job_id, profile_id=profile_id,
                        profile_version=profile_version, score=score,
                        missing=[], components={"fixture": "synthetic strong-match evidence"},
                    ))

            store.engine.dispose()
            store = Store(f"sqlite:///{database.as_posix()}")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external_requests = []
            blocked_writes = []
            accepted_profile_writes = []
            submission_requests = []
            explicit_add_started = False
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated title-suggestion API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_fixture_only(route):
                        nonlocal explicit_add_started
                        request = route.request
                        parsed = urlsplit(request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                        elif parsed.path.lower().endswith(("/submit", "/mock-run", "/apply")):
                            submission_requests.append((request.method, request.url))
                            route.abort()
                        elif request.method not in {"GET", "HEAD"}:
                            if (explicit_add_started and request.method == "PUT"
                                    and parsed.path == "/search-profiles"):
                                accepted_profile_writes.append(request)
                                route.continue_()
                            else:
                                blocked_writes.append((request.method, request.url))
                                route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_fixture_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    expect(page.get_by_role("heading", name="Suggested job titles", exact=True)).to_be_visible()

                    suggestion_profile = page.get_by_label(
                        "Search profile for suggested titles", exact=True)
                    suggestion_profile.select_option("fixture-search")
                    suggestions = page.get_by_role("list", name="Related title suggestions", exact=True)
                    expect(suggestions.get_by_text("Infrastructure Support Engineer", exact=True)).to_be_visible()
                    expect(suggestions.get_by_text("IT Operations Engineer", exact=True)).to_be_visible()
                    expect(suggestions.get_by_text("Junior Helpdesk Technician", exact=True)).to_have_count(0)
                    expect(suggestions.get_by_text("Legacy Systems Engineer", exact=True)).to_have_count(0)
                    expect(suggestions.get_by_text("System Administrator", exact=True)).to_have_count(0)
                    reason = suggestions.locator("li").filter(
                        has_text="Infrastructure Support Engineer").locator("p")
                    expect(reason).to_be_visible()
                    expect(reason).to_contain_text(re.compile(
                        r"1\s+current\s+strong\s+match", re.IGNORECASE))
                    add = page.get_by_role("button", name=(
                        "Add Infrastructure Support Engineer to Alex search"), exact=True)
                    expect(add).to_be_enabled()
                    expect(page.get_by_role("button", name=(
                        "Add Platform Administrator to Other search"), exact=True)).to_have_count(0)

                    before_display = page.request.get(
                        origin + "/search-profiles?profile_id=fixture").json()["search_profiles"]
                    fixture_before = next(row for row in before_display if row["id"] == "fixture-search")
                    self.assertEqual(fixture_before["criteria"]["include_titles"], ["System Administrator"])
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(accepted_profile_writes, [])

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("other")
                    other_selector = page.get_by_label("Search profile for suggested titles", exact=True)
                    expect(other_selector.locator("option", has_text="Alex search")).to_have_count(0)
                    other_selector.select_option("other-search")
                    other_suggestions = page.get_by_role(
                        "list", name="Related title suggestions", exact=True)
                    expect(other_suggestions.get_by_text("Platform Administrator", exact=True)).to_be_visible()
                    expect(other_suggestions.get_by_text("Infrastructure Support Engineer", exact=True)).to_have_count(0)
                    expect(other_suggestions.get_by_text("IT Operations Engineer", exact=True)).to_have_count(0)
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(accepted_profile_writes, [])

                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    fixture_selector = page.get_by_label(
                        "Search profile for suggested titles", exact=True)
                    fixture_selector.select_option("fixture-search")
                    explicit_add_started = True
                    with page.expect_request(lambda request: request.method == "PUT"
                            and urlsplit(request.url).path == "/search-profiles") as add_request:
                        page.get_by_role("button", name=(
                            "Add Infrastructure Support Engineer to Alex search"), exact=True).click()
                    self.assertIn("Infrastructure Support Engineer",
                                  add_request.value.post_data_json["include_titles"])
                    expect(page.get_by_role("button", name=(
                        "Add Infrastructure Support Engineer to Alex search"), exact=True)).to_have_count(0)
                    page.reload(wait_until="domcontentloaded")
                    readback = page.request.get(
                        origin + "/search-profiles/fixture-search").json()
                    self.assertEqual(readback["profile_id"], "fixture")
                    self.assertEqual(readback["criteria"]["include_titles"], [
                        "System Administrator", "Infrastructure Support Engineer"])
                    self.assertEqual(readback["criteria"]["remote"], "REQUIRED")
                    self.assertEqual(len(accepted_profile_writes), 1)
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(external_requests, [])
                    self.assertEqual(submission_requests, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated title-suggestion API did not stop")


if __name__ == "__main__":
    unittest.main()
