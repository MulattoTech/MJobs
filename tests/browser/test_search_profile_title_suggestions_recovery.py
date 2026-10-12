"""Connected browser recovery acceptance for search-profile title suggestions."""
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
class SearchProfileSuggestionRecoveryBrowserTests(unittest.TestCase):
    def test_failed_suggestions_load_can_retry_without_mutating_search_criteria(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-search-profile-suggestions-recovery-") as directory:
            root = Path(directory)
            database = root / "browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")

            save_search_profile(store, {
                "id": "fixture-search", "profile_id": "fixture", "name": "Alex search",
                "enabled": True, "include_titles": ["System Administrator"],
                "remote": "REQUIRED", "employment_types": [], "source_ids": [],
            })
            profile_version = store.profile("fixture")["version"]
            with store.engine.begin() as connection:
                template = dict(connection.execute(select(m.jobs).limit(1)).mappings().one())
                job_id = identity("browser-title-suggestion-recovery", "Infrastructure Support Engineer")
                connection.execute(m.jobs.insert().values(**{
                    **template, "id": job_id, "title": "Infrastructure Support Engineer",
                    "requisition": "suggestion-recovery-" + job_id[:16],
                }))
                connection.execute(m.matches.insert().values(
                    id=identity("browser-title-suggestion-recovery-match", job_id),
                    job_id=job_id, profile_id="fixture", profile_version=profile_version,
                    score=92, missing=[], components={"fixture": "synthetic strong-match evidence"},
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
            suggestion_attempts = []
            external_requests = []
            blocked_writes = []
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
                        request = route.request
                        parsed = urlsplit(request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                        elif request.method not in {"GET", "HEAD"}:
                            blocked_writes.append((request.method, request.url))
                            route.abort()
                        else:
                            route.continue_()

                    def fail_once_then_retry(route):
                        suggestion_attempts.append(route.request.url)
                        if len(suggestion_attempts) == 1:
                            route.fulfill(status=503, json={"detail": "synthetic suggestions outage"})
                        else:
                            route.continue_()

                    context.route("**/*", local_fixture_only)
                    context.route("**/profiles/fixture/search-profile-suggestions**",
                                  fail_once_then_retry)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    expect(page.get_by_role("heading", name="Suggested job titles", exact=True)).to_be_visible()
                    page.get_by_label("Search profile for suggested titles", exact=True).select_option(
                        "fixture-search")

                    suggestions_section = page.locator("section").filter(
                        has=page.get_by_role("heading", name="Suggested job titles", exact=True))
                    expect(suggestions_section.get_by_role("alert")).to_contain_text(
                        re.compile("suggest", re.IGNORECASE))
                    retry = suggestions_section.get_by_role(
                        "button", name=re.compile("retry", re.IGNORECASE))
                    expect(retry).to_be_visible()
                    self.assertEqual(len(suggestion_attempts), 1)

                    before_retry = page.request.get(
                        origin + "/search-profiles/fixture-search").json()
                    self.assertEqual(before_retry["criteria"]["include_titles"], ["System Administrator"])
                    self.assertEqual(blocked_writes, [])
                    retry.click()

                    suggestions = suggestions_section.get_by_role(
                        "list", name="Related title suggestions", exact=True)
                    expect(suggestions.get_by_text("Infrastructure Support Engineer", exact=True)).to_be_visible()
                    self.assertEqual(len(suggestion_attempts), 2)
                    after_retry = page.request.get(
                        origin + "/search-profiles/fixture-search").json()
                    self.assertEqual(after_retry["criteria"]["include_titles"], ["System Administrator"])
                    self.assertEqual(after_retry["criteria"]["remote"], "REQUIRED")
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(external_requests, [])
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
