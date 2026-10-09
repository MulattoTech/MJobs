"""Connected FIXTURE browser acceptance for search-profile title suggestions."""
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

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
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SearchProfileSuggestionsConnectedBrowserTests(unittest.TestCase):
    def test_suggestions_retry_explicit_add_and_profile_search_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-search-profile-suggestions-connected-") as directory:
            root = Path(directory)
            database = root / "browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                store.save_profile(dict(PROFILE))
                store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                    "email": "other@example.test"})
                with fixture_server() as source_origin:
                    discover(store, source_origin, "fixture")

                # Discovery persists employer sources separately from configured
                # search-policy sources. Inspect that registry, then configure one
                # isolated synthetic source whose ID is valid for saved searches.
                with store.engine.connect() as connection:
                    sources_after_discover = list(connection.execute(
                        select(m.job_sources.c.id).order_by(m.job_sources.c.id)).scalars())
                configured_source_id = (
                    sources_after_discover[0] if sources_after_discover else
                    configure_source(store, "fixture-board", provider="greenhouse")
                )
                with store.engine.connect() as connection:
                    registered_source_ids = set(connection.execute(
                        select(m.job_sources.c.id)).scalars())
                self.assertIn(configured_source_id, registered_source_ids)

                save_search_profile(store, {
                    "id": "fixture-search", "profile_id": "fixture", "name": "Alex search",
                    "enabled": False,
                    "include_titles": ["System Administrator", "Platform Administrator"],
                    "exclude_titles": ["Junior Helpdesk Technician"],
                    "required_keywords": ["Windows Server"],
                    "remote": "REQUIRED", "employment_types": ["FULL_TIME"],
                    "source_ids": [configured_source_id],
                    "job_filters": {
                        "q": "infrastructure operations", "remote_type": "REMOTE",
                        "provider": "greenhouse", "sort": "newest", "salary_min": "65000",
                    },
                })
                save_search_profile(store, {
                    "id": "other-search", "profile_id": "other", "name": "Other search",
                    "enabled": True, "include_titles": ["Network Administrator"],
                    "remote": "REQUIRED", "employment_types": [], "source_ids": [],
                })

                fixture_version = store.profile("fixture")["version"]
                other_version = store.profile("other")["version"]
                with store.engine.begin() as connection:
                    template = dict(connection.execute(select(m.jobs).limit(1)).mappings().one())
                    matches = (
                        ("Infrastructure Support Engineer", "fixture", fixture_version, 92),
                        ("Infrastructure Support Engineer", "fixture", fixture_version, 84),
                        ("IT Operations Engineer", "fixture", fixture_version, 82),
                        ("Junior Helpdesk Technician", "fixture", fixture_version, 74),
                        ("Legacy Systems Engineer", "fixture", "stale-profile-version", 99),
                        ("System Administrator", "fixture", fixture_version, 96),
                        ("Platform Administrator", "fixture", fixture_version, 90),
                        ("Desktop Support Analyst", "other", other_version, 90),
                    )
                    for index, (title, profile_id, profile_version, score) in enumerate(matches):
                        job_id = identity("browser-search-profile-suggestion", f"{index}:{title}")
                        connection.execute(m.jobs.insert().values(**{
                            **template, "id": job_id, "title": title,
                            "requisition": f"suggestion-{index}-{job_id[:16]}",
                        }))
                        connection.execute(m.matches.insert().values(
                            id=identity("browser-search-profile-suggestion-match", job_id),
                            job_id=job_id, profile_id=profile_id,
                            profile_version=profile_version, score=score, missing=[],
                            components={"fixture": "synthetic current strong-match evidence"},
                        ))
            finally:
                store.engine.dispose()

            store = Store(f"sqlite:///{database.as_posix()}")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            suggestion_requests = []
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

                    def local_fixture_with_retry_and_write_guard(route):
                        nonlocal explicit_add_started
                        request = route.request
                        parsed = urlsplit(request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external_requests.append(request.url)
                            route.abort()
                        elif any(segment in parsed.path.lower()
                                 for segment in ("/submit", "/mock-run", "/apply")):
                            submission_requests.append((request.method, request.url))
                            route.abort()
                        elif parsed.path == "/profiles/fixture/search-profile-suggestions":
                            suggestion_requests.append(request.url)
                            if len(suggestion_requests) == 1:
                                route.fulfill(status=503, json={
                                    "detail": "synthetic temporary suggestions outage"})
                            else:
                                route.continue_()
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

                    context.route("**/*", local_fixture_with_retry_and_write_guard)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    search_selector = page.get_by_label(
                        "Search profile for suggested titles", exact=True)
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == "/jobs"
                            and "search_profile_id=fixture-search" in response.url) as selected_search_response:
                        search_selector.select_option("fixture-search")
                    selected_search_query = parse_qs(
                        urlsplit(selected_search_response.value.url).query)
                    self.assertEqual(selected_search_query.get("q"), ["infrastructure operations"])
                    self.assertEqual(selected_search_query.get("remote_type"), ["REMOTE"])
                    self.assertEqual(selected_search_query.get("provider"), ["greenhouse"])
                    expect(page.get_by_label("Search jobs", exact=True)).to_have_value(
                        "infrastructure operations")
                    suggestions_section = page.locator("section").filter(
                        has=page.get_by_role("heading", name="Suggested job titles", exact=True))
                    expect(suggestions_section.get_by_role("alert")).to_contain_text(
                        re.compile("suggest", re.IGNORECASE))
                    retry = suggestions_section.get_by_role(
                        "button", name=re.compile("retry", re.IGNORECASE))
                    expect(retry).to_be_visible()
                    self.assertEqual(len(suggestion_requests), 1)

                    def read_search(search_id):
                        return page.request.get(origin + f"/search-profiles/{search_id}").json()

                    before_retry = read_search("fixture-search")
                    original_criteria = before_retry["criteria"]
                    original_source_ids = before_retry["source_ids"]
                    self.assertEqual(original_criteria["include_titles"], [
                        "System Administrator", "Platform Administrator"])
                    self.assertEqual(original_criteria["remote"], "REQUIRED")
                    self.assertEqual(original_criteria["employment_types"], ["FULL_TIME"])
                    self.assertEqual(original_criteria["exclude_titles"], ["Junior Helpdesk Technician"])
                    self.assertEqual(original_source_ids, [configured_source_id])
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(accepted_profile_writes, [])

                    with page.expect_response(lambda response:
                            urlsplit(response.url).path ==
                            "/profiles/fixture/search-profile-suggestions") as retried_response:
                        retry.click()
                    self.assertEqual(retried_response.value.status, 200)
                    fixture_request = next(
                        url for url in reversed(suggestion_requests)
                        if urlsplit(url).path == "/profiles/fixture/search-profile-suggestions")
                    self.assertEqual(
                        parse_qs(urlsplit(fixture_request).query).get("search_profile_id"),
                        ["fixture-search"],
                        "Suggestions must be scoped to the selected saved search profile",
                    )
                    suggestions = suggestions_section.get_by_role(
                        "list", name="Related title suggestions", exact=True)
                    infrastructure = suggestions.locator("li").filter(
                        has_text="Infrastructure Support Engineer")
                    expect(infrastructure).to_be_visible()
                    expect(infrastructure.locator("[aria-label='Strong-match evidence']")).to_contain_text(
                        re.compile(r"2\s+current\s+strong\s+matches", re.IGNORECASE))
                    expect(suggestions.get_by_text("IT Operations Engineer", exact=True)).to_be_visible()
                    for excluded in (
                            "Junior Helpdesk Technician", "Legacy Systems Engineer",
                            "System Administrator", "Platform Administrator"):
                        expect(suggestions.get_by_text(excluded, exact=True)).to_have_count(0)
                    self.assertEqual(len(suggestion_requests), 2)
                    self.assertEqual(read_search("fixture-search")["criteria"], original_criteria)
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(accepted_profile_writes, [])

                    foreign_search = page.request.get(
                        origin + "/profiles/fixture/search-profile-suggestions"
                        "?search_profile_id=other-search")
                    self.assertGreaterEqual(
                        foreign_search.status, 400,
                        "A search profile owned by another career profile must fail closed",
                    )

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("other")
                    other_selector = page.get_by_label(
                        "Search profile for suggested titles", exact=True)
                    expect(other_selector.locator("option", has_text="Alex search")).to_have_count(0)
                    other_selector.select_option("other-search")
                    other_suggestions = page.get_by_role(
                        "list", name="Related title suggestions", exact=True)
                    expect(other_suggestions.get_by_text("Desktop Support Analyst", exact=True)).to_be_visible()
                    expect(other_suggestions.get_by_text("Infrastructure Support Engineer", exact=True)).to_have_count(0)
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(accepted_profile_writes, [])

                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    search_selector = page.get_by_label(
                        "Search profile for suggested titles", exact=True)
                    search_selector.select_option("fixture-search")
                    add = page.get_by_role("button", name=(
                        "Add Infrastructure Support Engineer to Alex search"), exact=True)
                    expect(add).to_be_enabled()
                    explicit_add_started = True
                    with page.expect_request(lambda request: request.method == "PUT"
                            and urlsplit(request.url).path == "/search-profiles") as add_request:
                        add.click()
                    self.assertEqual(add_request.value.post_data_json["profile_id"], "fixture")
                    self.assertEqual(add_request.value.post_data_json["include_titles"], [
                        "System Administrator", "Platform Administrator",
                        "Infrastructure Support Engineer"])
                    self.assertEqual(add_request.value.post_data_json["source_ids"], [configured_source_id])
                    expect(page.get_by_role("button", name=(
                        "Add Infrastructure Support Engineer to Alex search"), exact=True)).to_have_count(0)
                    self.assertEqual(len(accepted_profile_writes), 1)

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    durable = read_search("fixture-search")
                    self.assertEqual(durable["profile_id"], "fixture")
                    self.assertEqual(durable["criteria"]["include_titles"], [
                        "System Administrator", "Platform Administrator",
                        "Infrastructure Support Engineer"])
                    self.assertEqual(durable["criteria"]["remote"], "REQUIRED")
                    self.assertEqual(durable["criteria"]["employment_types"], ["FULL_TIME"])
                    self.assertEqual(durable["criteria"]["exclude_titles"], [
                        "Junior Helpdesk Technician"])
                    self.assertEqual(durable["criteria"]["required_keywords"], ["Windows Server"])
                    self.assertEqual(durable["criteria"]["job_filters"], original_criteria["job_filters"])
                    self.assertEqual(durable["source_ids"], original_source_ids)

                    saved_search = page.get_by_label("Saved search profile", exact=True)
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == "/jobs"
                            and "search_profile_id=fixture-search" in response.url) as restored_search_response:
                        saved_search.select_option("fixture-search")
                    restored_search_query = parse_qs(
                        urlsplit(restored_search_response.value.url).query)
                    self.assertEqual(restored_search_query.get("q"), ["infrastructure operations"])
                    self.assertEqual(restored_search_query.get("remote_type"), ["REMOTE"])
                    self.assertEqual(restored_search_query.get("provider"), ["greenhouse"])
                    expect(page.get_by_label("Search jobs", exact=True)).to_have_value(
                        "infrastructure operations")
                    expect(page.get_by_label("Saved search profile", exact=True)).to_have_value(
                        "fixture-search")
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
