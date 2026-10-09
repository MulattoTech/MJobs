"""Connected FIXTURE acceptance for profile-scoped title suggestions and confirmation."""
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
class SearchProfileSuggestionsProfileScopeBrowserTests(unittest.TestCase):
    def test_suggestion_confirmation_is_read_back_and_scoped_to_selected_profile_search(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-title-suggestions-profile-scope-") as directory:
            root = Path(directory)
            database = root / "browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            self.addCleanup(store.engine.dispose)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                "email": "other@example.test"})
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")

            configured_source_id = configure_source(
                store, "fixture-board", provider="greenhouse",
            )
            search_inputs = (
                ("fixture-alpha", "Alpha search", "fixture", ["System Administrator"],
                 [configured_source_id]),
                ("fixture-beta", "Beta search", "fixture", ["Cloud Administrator"],
                 [configured_source_id]),
                ("other-search", "Other search", "other", ["Network Administrator"], []),
            )
            for identifier, name, profile_id, titles, source_ids in search_inputs:
                save_search_profile(store, {
                    "id": identifier,
                    "profile_id": profile_id,
                    "name": name,
                    "enabled": False,
                    "include_titles": titles,
                    "exclude_titles": ["Junior Helpdesk Technician"],
                    "required_keywords": ["Windows Server"],
                    "remote": "REQUIRED",
                    "employment_types": ["FULL_TIME"],
                    "source_ids": source_ids,
                    "job_filters": {
                        "q": f"query for {name}", "remote_type": "REMOTE",
                        "provider": "greenhouse", "sort": "newest",
                    },
                })

            fixture_version = store.profile("fixture")["version"]
            other_version = store.profile("other")["version"]
            synthetic_matches = (
                ("Infrastructure Support Engineer", "fixture", fixture_version, 92),
                ("Infrastructure Support Engineer", "fixture", fixture_version, 84),
                ("IT Operations Engineer", "fixture", fixture_version, 82),
                ("Cloud Administrator", "fixture", fixture_version, 91),
                ("Junior Helpdesk Technician", "fixture", fixture_version, 74),
                ("Legacy Systems Engineer", "fixture", "stale-profile-version", 99),
                ("System Administrator", "fixture", fixture_version, 96),
                ("Desktop Support Analyst", "other", other_version, 90),
            )
            with store.engine.begin() as connection:
                template = dict(connection.execute(select(m.jobs).limit(1)).mappings().one())
                for index, (title, profile_id, profile_version, score) in enumerate(synthetic_matches):
                    job_id = identity("browser-suggestion-profile-scope", f"{index}:{title}")
                    connection.execute(m.jobs.insert().values(**{
                        **template,
                        "id": job_id,
                        "title": title,
                        "requisition": f"suggestion-scope-{index}-{job_id[:14]}",
                    }))
                    connection.execute(m.matches.insert().values(
                        id=identity("browser-suggestion-profile-scope-match", job_id),
                        job_id=job_id,
                        profile_id=profile_id,
                        profile_version=profile_version,
                        score=score,
                        missing=[],
                        components={"fixture": "synthetic current strong-match evidence"},
                    ))

            store.engine.dispose()
            store = Store(f"sqlite:///{database.as_posix()}")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error",
            ))
            server_thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            external_requests = []
            submission_requests = []
            blocked_writes = []
            accepted_profile_writes = []
            network_events = []
            explicit_add_started = False
            try:
                server_thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not server_thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated title-suggestion API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_fixture_with_write_guard(route):
                        nonlocal explicit_add_started
                        request = route.request
                        parsed = urlsplit(request.url)
                        network_events.append((request.method, parsed.path, parsed.query))
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

                    context.route("**/*", local_fixture_with_write_guard)
                    page = context.new_page()
                    page.set_default_timeout(9000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    selector = page.get_by_label("Search profile for suggested titles", exact=True)
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path ==
                            "/profiles/fixture/search-profile-suggestions"
                            and parse_qs(urlsplit(response.url).query).get("search_profile_id") ==
                            ["fixture-alpha"]):
                        selector.select_option("fixture-alpha")
                    suggestions_section = page.locator("section").filter(
                        has=page.get_by_role("heading", name="Suggested job titles", exact=True))
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
                        "System Administrator", "Desktop Support Analyst",
                    ):
                        expect(suggestions.get_by_text(excluded, exact=True)).to_have_count(0)

                    def read_search(search_id):
                        return page.request.get(origin + f"/search-profiles/{search_id}").json()

                    alpha_before = read_search("fixture-alpha")
                    beta_before = read_search("fixture-beta")
                    original_alpha_criteria = alpha_before["criteria"]
                    original_beta_criteria = beta_before["criteria"]
                    self.assertEqual(alpha_before["profile_id"], "fixture")
                    self.assertEqual(original_alpha_criteria["include_titles"], ["System Administrator"])
                    self.assertEqual(original_alpha_criteria["exclude_titles"], ["Junior Helpdesk Technician"])
                    self.assertEqual(original_alpha_criteria["required_keywords"], ["Windows Server"])
                    self.assertEqual(original_alpha_criteria["remote"], "REQUIRED")
                    self.assertEqual(original_alpha_criteria["job_filters"]["q"], "query for Alpha search")
                    self.assertEqual(beta_before["profile_id"], "fixture")
                    self.assertEqual(original_beta_criteria["include_titles"], ["Cloud Administrator"])
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(accepted_profile_writes, [])
                    expect(page.get_by_role("status").filter(
                        has_text=re.compile("Added Infrastructure Support Engineer"))).to_have_count(0)

                    # A foreign search ID must not leak its criteria or suggestions into fixture.
                    foreign = page.request.get(
                        origin + "/profiles/fixture/search-profile-suggestions"
                        "?search_profile_id=other-search")
                    self.assertEqual(foreign.status, 404, foreign.text())
                    self.assertEqual(foreign.json()["detail"], {
                        "code": "SEARCH_PROFILE_SUGGESTIONS_SEARCH_NOT_FOUND",
                    })

                    # A deliberate click adds one title, then waits for server readback.
                    explicit_add_started = True
                    add_alpha = page.get_by_role("button", name=(
                        "Add Infrastructure Support Engineer to Alpha search"), exact=True)
                    with page.expect_request(lambda request: request.method == "PUT"
                            and urlsplit(request.url).path == "/search-profiles") as add_request:
                        add_alpha.click()
                    self.assertEqual(add_request.value.post_data_json["id"], "fixture-alpha")
                    self.assertEqual(add_request.value.post_data_json["profile_id"], "fixture")
                    self.assertEqual(add_request.value.post_data_json["include_titles"], [
                        "System Administrator", "Infrastructure Support Engineer"])
                    self.assertEqual(add_request.value.post_data_json["source_ids"], [configured_source_id])
                    success_notice = page.get_by_role("status").filter(
                        has_text="Added Infrastructure Support Engineer to Alpha search.")
                    expect(success_notice).to_be_visible()
                    alpha_after = read_search("fixture-alpha")
                    beta_after = read_search("fixture-beta")
                    self.assertEqual(alpha_after["profile_id"], "fixture")
                    self.assertEqual(alpha_after["criteria"]["include_titles"], [
                        "System Administrator", "Infrastructure Support Engineer"])
                    for key, value in original_alpha_criteria.items():
                        if key != "include_titles":
                            self.assertEqual(alpha_after["criteria"][key], value)
                    self.assertEqual(beta_after["criteria"], original_beta_criteria)
                    self.assertEqual(len(accepted_profile_writes), 1)
                    put_index = next(index for index, event in enumerate(network_events)
                                     if event[0] == "PUT" and event[1] == "/search-profiles")
                    readback_index = next(index for index, event in enumerate(network_events)
                                          if event[0] == "GET" and event[1] == "/search-profiles"
                                          and parse_qs(event[2]).get("profile_id") == ["fixture"]
                                          and index > put_index)
                    self.assertGreater(readback_index, put_index)

                    # Beta lacks this title; changing searches must replace the view and clear
                    # Alpha's success confirmation while keeping Beta's saved criteria intact.
                    selector = page.get_by_label("Search profile for suggested titles", exact=True)
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path ==
                            "/profiles/fixture/search-profile-suggestions"
                            and parse_qs(urlsplit(response.url).query).get("search_profile_id") ==
                            ["fixture-beta"]):
                        selector.select_option("fixture-beta")
                    beta_suggestions = suggestions_section.get_by_role(
                        "list", name="Related title suggestions", exact=True)
                    expect(beta_suggestions.get_by_text(
                        "Infrastructure Support Engineer", exact=True)).to_be_visible()
                    alpha_confirmation_after_search_switch = success_notice.count()
                    self.assertEqual(read_search("fixture-beta")["criteria"], original_beta_criteria)

                    # Changing career profiles clears both the old confirmation and old
                    # suggestions; the new profile sees only its own strong-match evidence.
                    page.get_by_label("Active profile", exact=True).select_option("other")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("other")
                    other_selector = page.get_by_label("Search profile for suggested titles", exact=True)
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path ==
                            "/profiles/other/search-profile-suggestions"
                            and parse_qs(urlsplit(response.url).query).get("search_profile_id") ==
                            ["other-search"]):
                        other_selector.select_option("other-search")
                    other_suggestions = page.get_by_role(
                        "list", name="Related title suggestions", exact=True)
                    expect(other_suggestions.get_by_text(
                        "Desktop Support Analyst", exact=True)).to_be_visible()
                    expect(other_suggestions.get_by_text(
                        "Infrastructure Support Engineer", exact=True)).to_have_count(0)
                    alpha_confirmation_after_profile_switch = success_notice.count()
                    self.assertEqual(read_search("fixture-alpha")["criteria"]["include_titles"], [
                        "System Administrator", "Infrastructure Support Engineer"])
                    self.assertEqual(read_search("other-search")["profile_id"], "other")
                    self.assertEqual(blocked_writes, [])
                    self.assertEqual(external_requests, [])
                    self.assertEqual(submission_requests, [])
                    self.assertEqual({
                        "confirmation after search switch": alpha_confirmation_after_search_switch,
                        "confirmation after profile switch": alpha_confirmation_after_profile_switch,
                    }, {
                        "confirmation after search switch": 0,
                        "confirmation after profile switch": 0,
                    }, "An add confirmation must not remain attached to another search or profile")
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                server_thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(server_thread.is_alive(), "Isolated suggestion API did not stop")


if __name__ == "__main__":
    unittest.main()
