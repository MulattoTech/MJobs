"""Connected Chromium acceptance for saved-search cadence editing."""
from pathlib import Path
import json
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
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.scheduler import schedule_due_sources
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SavedSearchScheduleBrowserTests(unittest.TestCase):
    def test_legacy_search_keeps_source_policy_after_edit_pause_and_resume(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-source-policy-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            source_id = configure_source(store, "legacy-schedule-fixture", jitter_seconds=0)
            search_id = "legacy-source-policy"
            search_path = f"/search-profiles/{search_id}"
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error", timeout_graceful_shutdown=3,
            ))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            unexpected = []

            def local_only(route):
                request = route.request
                if (urlsplit(request.url).netloc != urlsplit(origin).netloc
                        or request.method not in ("GET", "PUT")):
                    unexpected.append((request.method, request.url))
                    route.abort()
                else:
                    route.continue_()

            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated source-policy API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.route("**/*", local_only)
                    saved = context.request.put(origin + "/search-profiles", data={
                        "id": search_id, "profile_id": "fixture", "name": "Legacy source-policy search",
                        "enabled": True, "include_titles": ["Systems Administrator"], "source_ids": [source_id],
                    })
                    self.assertEqual(saved.status, 200, saved.text())

                    def read_search():
                        response = context.request.get(origin + search_path)
                        self.assertEqual(response.status, 200, response.text())
                        return response.json()

                    self.assertNotIn("scheduler", read_search()["criteria"])
                    page = context.new_page()
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    page.locator(".document").filter(has_text="Legacy source-policy search").get_by_role(
                        "button", name="Edit search", exact=True,
                    ).click()
                    page.get_by_label("Search name", exact=True).fill("Renamed source-policy search")

                    def update_search(button, enabled):
                        with page.expect_response(lambda response: response.url == origin + "/search-profiles"
                                                  and response.request.method == "PUT") as updated:
                            button.click()
                        self.assertEqual(updated.value.status, 200, updated.value.text())
                        self.assertNotIn("scheduler", updated.value.request.post_data_json)
                        record = read_search()
                        self.assertNotIn("scheduler", record["criteria"])
                        self.assertEqual(record["name"], "Renamed source-policy search")
                        self.assertEqual(record["source_ids"], [source_id])
                        self.assertEqual(record["criteria"]["include_titles"], ["Systems Administrator"])
                        self.assertIs(record["enabled"], enabled)

                    update_search(page.get_by_role("button", name="Update search", exact=True), True)
                    card = page.locator(".document").filter(has_text="Renamed source-policy search")
                    update_search(card.get_by_role("button", name="Pause search", exact=True), False)
                    self.assertEqual(schedule_due_sources(store)["scheduled"], [])
                    self.assertEqual(store.count("worker_operations"), 0)
                    update_search(card.get_by_role("button", name="Resume search", exact=True), True)
                    due = schedule_due_sources(store)
                    self.assertEqual(due["saved_searches"]["scheduled"], [])
                    run, = due["scheduled"]
                    self.assertEqual((run["source_id"], run["profile_ids"]), (source_id, ["fixture"]))
                    with store.engine.connect() as connection:
                        operation = connection.execute(select(m.operations)).mappings().one()
                    self.assertEqual(operation["id"], run["operation_id"])
                    self.assertEqual(operation["state"], "QUEUED")
                    self.assertEqual(operation["params"]["scheduler"], "source-policy-v1")
                    self.assertEqual(unexpected, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=15)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated source-policy API did not stop")

    def test_saved_search_cadence_persists_and_never_starts_external_work(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-saved-search-schedule-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            self.addCleanup(store.engine.dispose)
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                profile_id = "schedule-browser-profile"
                store.save_profile({**PROFILE, "id": profile_id, "name": "Scheduler Fixture",
                                    "email": "scheduler@example.test"})
                source_id = configure_source(
                    store, "browser-scheduler-source", provider="greenhouse", enabled=True,
                    poll_interval=1800, jitter_seconds=0,
                )
                with store.engine.connect() as connection:
                    source_before = dict(connection.execute(select(m.job_sources).where(
                        m.job_sources.c.id == source_id)).mappings().one())
                jobs_before = store.count("canonical_jobs")
                postings_before = store.count("job_postings")
                operations_before = store.count("worker_operations")
            except BaseException:
                store.engine.dispose()
                raise
            store.engine.dispose()
            store = Store(database_url)
            self.addCleanup(store.engine.dispose)

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
                        self.fail("Isolated saved-search scheduler API did not start")
                    time.sleep(0.02)

                external = []
                requests = []
                search_profile_puts = []
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
                    def capture_request(request):
                        requests.append((request.method, request.url))
                        if request.method == "PUT" and urlsplit(request.url).path == "/search-profiles":
                            search_profile_puts.append(json.loads(request.post_data or "{}"))

                    context.on("request", capture_request)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate(f"localStorage.setItem('mjobs-profile', {profile_id!r})")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value(profile_id)
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(page.get_by_role("heading", name="Scheduled search profiles")).to_be_visible()
                    expect(page.get_by_role("heading", name="browser-scheduler-source")).to_be_visible()

                    search_name = "Local schedule acceptance"
                    page.get_by_label("Search name", exact=True).fill(search_name)
                    page.get_by_label("Include titles", exact=True).fill("Synthetic administrator")
                    page.get_by_role("button", name="Save search", exact=True).click()
                    schedule = page.get_by_role("region", name=f'Schedule “{search_name}”')
                    expect(schedule).to_be_visible()
                    enabled = schedule.get_by_label("Enable scheduled discovery", exact=True)
                    interval = schedule.get_by_label("Interval (seconds)", exact=True)
                    expect(enabled).not_to_be_checked()
                    expect(interval).to_have_value("3600")

                    # Enabling and changing cadence is a settings write only.
                    enabled.check()
                    interval.fill("900")
                    page.get_by_role("button", name="Save schedule", exact=True).click()
                    expect(page.get_by_role("button", name="Save schedule", exact=True)).to_have_count(0)
                    expect(enabled).to_be_checked()
                    expect(interval).to_have_value("900")
                    persisted = page.request.get(
                        origin + "/search-profiles", params={"profile_id": profile_id})
                    self.assertEqual(persisted.status, 200, persisted.text())
                    record, = [row for row in persisted.json()["search_profiles"]
                               if row["name"] == search_name]
                    self.assertEqual(record["criteria"]["scheduler"],
                                     {"enabled": True, "interval_seconds": 900})
                    self.assertEqual(record["source_ids"], [])
                    self.assertEqual(record["criteria"]["include_titles"], ["Synthetic administrator"])
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    schedule = page.get_by_role("region", name=f'Schedule “{search_name}”')
                    expect(schedule.get_by_label("Enable scheduled discovery", exact=True)).to_be_checked()
                    expect(schedule.get_by_label("Interval (seconds)", exact=True)).to_have_value("900")
                    search_card = page.locator(".document").filter(has=schedule)
                    with page.expect_response(lambda response: response.url == origin + "/search-profiles"
                                              and response.request.method == "PUT") as pause:
                        search_card.get_by_role("button", name="Pause search", exact=True).click()
                    self.assertEqual(pause.value.status, 200, pause.value.text())
                    self.assertNotIn("scheduler", pause.value.request.post_data_json)
                    expect(search_card.get_by_text("Search paused", exact=True)).to_be_visible()
                    expect(search_card.get_by_text("Resume this search to collect jobs with it.")).to_be_visible()
                    expect(search_card.get_by_role("button", name="Collect for this search", exact=True)).to_be_disabled()
                    with page.expect_response(lambda response: response.url == origin + "/search-profiles"
                                              and response.request.method == "PUT") as resume:
                        search_card.get_by_role("button", name="Resume search", exact=True).click()
                    self.assertEqual(resume.value.status, 200, resume.value.text())
                    self.assertNotIn("scheduler", resume.value.request.post_data_json)
                    expect(search_card.get_by_text("Search enabled", exact=True)).to_be_visible()
                    expect(search_card.get_by_role(
                        "button", name=f"Collect {search_name} from browser-scheduler-source", exact=True,
                    )).to_be_enabled()
                    expect(schedule.get_by_label("Enable scheduled discovery", exact=True)).to_be_checked()
                    expect(schedule.get_by_label("Interval (seconds)", exact=True)).to_have_value("900")

                    # A separate interval edit persists, then the user can turn scheduling off.
                    schedule.get_by_label("Interval (seconds)", exact=True).fill("1200")
                    page.get_by_role("button", name="Save schedule", exact=True).click()
                    expect(page.get_by_role("button", name="Save schedule", exact=True)).to_have_count(0)
                    expect(enabled).to_be_checked()
                    expect(interval).to_have_value("1200")
                    disabled = schedule.get_by_label("Enable scheduled discovery", exact=True)
                    disabled.uncheck()
                    expect(disabled).not_to_be_checked()
                    page.get_by_role("button", name="Save schedule", exact=True).click()
                    expect(page.get_by_role("button", name="Save schedule", exact=True)).to_have_count(0)
                    expect(disabled).not_to_be_checked()
                    expect(interval).to_have_value("1200")
                    disable_payload = search_profile_puts[-1]
                    self.assertEqual(disable_payload.get("scheduler"),
                                     {"enabled": False, "interval_seconds": 1200},
                                     json.dumps(disable_payload, sort_keys=True))
                    persisted = page.request.get(
                        origin + "/search-profiles", params={"profile_id": profile_id})
                    record, = [row for row in persisted.json()["search_profiles"]
                               if row["name"] == search_name]
                    self.assertEqual(record["criteria"]["scheduler"],
                                     {"enabled": False, "interval_seconds": 1200})
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    schedule = page.get_by_role("region", name=f'Schedule “{search_name}”')
                    expect(schedule.get_by_label("Enable scheduled discovery", exact=True)).not_to_be_checked()
                    expect(schedule.get_by_label("Interval (seconds)", exact=True)).to_have_value("1200")

                    limit_window = schedule.get_by_label("Limit discovery to a local time window", exact=True)
                    limit_window.check()
                    schedule.get_by_label("IANA time zone", exact=True).fill("America/Chicago")
                    schedule.get_by_label("Starts at (inclusive)", exact=True).fill("22:00")
                    schedule.get_by_label("Ends at (exclusive)", exact=True).fill("06:00")
                    save_schedule = page.get_by_role("button", name="Save schedule", exact=True)
                    save_schedule.click()
                    expect(save_schedule).to_have_count(0)
                    self.assertEqual(search_profile_puts[-1]["scheduler"], {
                        "enabled": False, "interval_seconds": 1200,
                        "window": {"time_zone": "America/Chicago", "start_local": "22:00", "end_local": "06:00"},
                    })

                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(limit_window).to_be_checked()
                    expect(schedule.get_by_label("IANA time zone", exact=True)).to_have_value("America/Chicago")
                    expect(schedule.get_by_label("Starts at (inclusive)", exact=True)).to_have_value("22:00")
                    expect(schedule.get_by_label("Ends at (exclusive)", exact=True)).to_have_value("06:00")
                    saved_window_rule = search_profile_puts[-1]["scheduler"]
                    page.locator(".document").filter(has=schedule).get_by_role(
                        "button", name="Edit search", exact=True,
                    ).click()
                    search_name = "Renamed schedule acceptance"
                    page.get_by_label("Search name", exact=True).fill(search_name)
                    with page.expect_response(lambda response: response.url == origin + "/search-profiles"
                                              and response.request.method == "PUT") as renamed:
                        page.get_by_role("button", name="Update search", exact=True).click()
                    self.assertEqual(renamed.value.status, 200, renamed.value.text())
                    self.assertNotIn("scheduler", renamed.value.request.post_data_json)
                    persisted = page.request.get(origin + f"/search-profiles/{record['id']}")
                    self.assertEqual(persisted.status, 200, persisted.text())
                    self.assertEqual(persisted.json()["criteria"]["scheduler"], saved_window_rule)
                    schedule = page.get_by_role("region", name=f'Schedule “{search_name}”')
                    limit_window = schedule.get_by_label("Limit discovery to a local time window", exact=True)
                    expect(limit_window).to_be_checked()
                    limit_window.uncheck()
                    save_schedule.click()
                    expect(save_schedule).to_have_count(0)
                    self.assertEqual(search_profile_puts[-1]["scheduler"],
                                     {"enabled": False, "interval_seconds": 1200})

                    self.assertEqual(external, [])
                    local_requests = [(method, urlsplit(url).path)
                                      for method, url in requests if url.startswith(origin)]
                    self.assertFalse(any(path.startswith(f"/sources/{source_id}/collect")
                                         or path == "/discovery"
                                         for _, path in local_requests), local_requests)
                    self.assertFalse(any(method == "POST" and (
                        "submit" in path.casefold() or "/apply" in path.casefold()
                        or path == "/applications") for method, path in local_requests),
                        local_requests)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                if thread.is_alive():
                    server.force_exit = True
                    thread.join(timeout=5)
                sock.close()
                store.engine.dispose()

            try:
                self.assertFalse(thread.is_alive(), "Isolated saved-search scheduler API did not stop")
                self.assertEqual(store.count("worker_operations"), operations_before)
                self.assertEqual(store.count("canonical_jobs"), jobs_before)
                self.assertEqual(store.count("job_postings"), postings_before)
                with store.engine.connect() as connection:
                    source_after = dict(connection.execute(select(m.job_sources).where(
                        m.job_sources.c.id == source_id)).mappings().one())
                self.assertEqual(source_after["last_attempt"], source_before["last_attempt"])
                self.assertEqual(source_after["poll_interval"], source_before["poll_interval"])
            finally:
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
