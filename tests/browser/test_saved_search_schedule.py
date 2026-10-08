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
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SavedSearchScheduleBrowserTests(unittest.TestCase):
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
