"""Connected Chromium acceptance for application capability filtering."""
from pathlib import Path
import os
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE

ROOT = Path(os.environ.get("MJOBS_BROWSER_APP_ROOT", Path(__file__).resolve().parents[2]))


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationCapabilityFilterBrowserTests(unittest.TestCase):
    def test_filter_is_connected_saved_profile_scoped_and_local_only(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-application-capability-browser-") as directory:
            root = Path(directory)
            database = root / "browser.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                "email": "other@example.test"})
            self._seed_routes(store)
            operations_before = store.count("worker_operations")
            applications_before = store.count("applications")
            attempts_before = store.count("application_attempts")

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            store.engine.dispose()
            store = Store(f"sqlite:///{database.as_posix()}")
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error"))
            thread = threading.Thread(target=server.run,
                                      kwargs={"sockets": [sock]}, daemon=True)
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated application-capability API did not start")
                    time.sleep(0.02)

                external = []
                unexpected_writes = []
                submission_requests = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_read_only(route):
                        request = route.request
                        if urlsplit(request.url).netloc != urlsplit(origin).netloc:
                            external.append(request.url)
                            route.abort()
                        elif (request.method not in {"GET", "HEAD"}
                              and any(path in urlsplit(request.url).path.lower()
                                      for path in ("/mock-run", "/submit", "/apply"))):
                            submission_requests.append((request.method, request.url))
                            route.abort()
                        elif request.method not in {"GET", "HEAD"} and not (
                                request.method == "PUT"
                                and urlsplit(request.url).path == "/search-profiles"):
                            unexpected_writes.append((request.method, request.url))
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_read_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    filters = page.locator(".filters")
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    capability = page.get_by_label("Application capability", exact=True)
                    expect(capability).to_be_visible()
                    values = capability.locator("option").evaluate_all(
                        "options => options.map(option => option.value).filter(Boolean)")
                    self.assertEqual(set(values), {
                        "FIXTURE_ONLY", "UNSUPPORTED", "REVIEW_REQUIRED", "UNKNOWN",
                    })

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_capability=FIXTURE_ONLY" in response.url) as fixture_response:
                        capability.select_option("FIXTURE_ONLY")
                    self.assertEqual(fixture_response.value.status, 200)
                    self.assertEqual([row["id"] for row in fixture_response.value.json()["jobs"]],
                                     ["browser-job-fixture"])
                    expect(page.locator(".job-card").filter(has_text="B Fixture mock")).to_be_visible()

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_capability=UNSUPPORTED" in response.url) as unsupported_response:
                        capability.select_option("UNSUPPORTED")
                    self.assertEqual(unsupported_response.value.status, 200)
                    self.assertEqual({row["id"] for row in unsupported_response.value.json()["jobs"]},
                                     {"browser-job-unsupported", "browser-job-exact-mismatch"})

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_capability=REVIEW_REQUIRED" in response.url) as review_response:
                        capability.select_option("REVIEW_REQUIRED")
                    self.assertEqual([row["id"] for row in review_response.value.json()["jobs"]],
                                     ["browser-job-review"])

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_capability=UNKNOWN" in response.url) as unknown_response:
                        capability.select_option("UNKNOWN")
                    self.assertEqual([row["id"] for row in unknown_response.value.json()["jobs"]],
                                     ["browser-job-unknown"])

                    page.get_by_label("Saved search name", exact=True).fill("Fixture routes")
                    capability.select_option("FIXTURE_ONLY")
                    page.get_by_role("button", name="Save current filters", exact=True).click()
                    saved = filters.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Fixture routes")).to_have_count(1)
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    saved = filters.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Fixture routes")).to_have_count(1)
                    selected_value = saved.locator(
                        "option", has_text="Fixture routes").get_attribute("value")
                    self.assertTrue(selected_value, "Saved search option must expose its persisted ID")
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and f"search_profile_id={selected_value}" in response.url
                            and "application_capability=FIXTURE_ONLY" in response.url) as restored:
                        saved.select_option(selected_value)
                    self.assertEqual([row["id"] for row in restored.value.json()["jobs"]],
                                     ["browser-job-fixture"])
                    expect(capability).to_have_value("FIXTURE_ONLY")

                    # An explicit empty override clears the selected filter while preserving
                    # the saved search value for the next selection/reload.
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and f"search_profile_id={selected_value}" in response.url
                            and "application_capability=" in response.url
                            and "application_capability=FIXTURE_ONLY" not in response.url) as cleared:
                        capability.select_option("")
                    self.assertEqual(cleared.value.status, 200)
                    visible = {row["id"] for row in cleared.value.json()["jobs"]}
                    self.assertTrue({
                        "browser-job-fixture", "browser-job-unsupported",
                        "browser-job-review", "browser-job-unknown",
                    } <= visible)
                    persisted = page.request.get(origin + "/search-profiles?profile_id=fixture")
                    self.assertEqual(persisted.status, 200)
                    row = next(item for item in persisted.json()["search_profiles"]
                               if item["id"] == selected_value)
                    self.assertEqual(row["criteria"]["job_filters"]["application_capability"],
                                     "FIXTURE_ONLY")

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    other_searches = filters.get_by_label("Saved search profile", exact=True)
                    expect(other_searches).to_have_value("")
                    expect(other_searches.locator("option", has_text="Fixture routes")).to_have_count(0)
                    self.assertEqual(external, [])
                    self.assertEqual(unexpected_writes, [])
                    self.assertEqual(submission_requests, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated application-capability API did not stop")
            reopened = Store(f"sqlite:///{database.as_posix()}")
            try:
                self.assertEqual(reopened.count("worker_operations"), operations_before)
                self.assertEqual(reopened.count("applications"), applications_before)
                self.assertEqual(reopened.count("application_attempts"), attempts_before)
            finally:
                reopened.engine.dispose()

    def _seed_routes(self, store):
        routes = (
            ("unsupported", "A Unsupported greenhouse", "greenhouse", "board-a"),
            ("fixture", "B Fixture mock", "mock-ats", "fixture-board"),
            ("review", "C Review required", "unrecognized-provider", "weak-board"),
            ("unknown", "D Unknown route", "unrecognized-provider", "unknown-board"),
            ("exact-mismatch", "E Exact route mismatch", "greenhouse", "route-board"),
        )
        with store.engine.begin() as connection:
            for suffix, title, provider, tenant in routes:
                company_id = f"browser-company-{suffix}"
                job_id = f"browser-job-{suffix}"
                upsert(connection, m.companies, {
                    "id": company_id, "name": title, "domain": "",
                })
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": company_id, "requisition": suffix,
                    "title": title, "description": "Synthetic browser acceptance fixture.",
                    "skills": [], "location": "Remote", "remote_type": "REMOTE",
                })
                upsert(connection, m.routes, {
                    "id": f"{job_id}-route", "job_id": job_id, "company_id": company_id,
                    "provider": provider, "tenant": tenant, "requisition": suffix,
                    "url": f"https://jobs.example.test/apply/{suffix}",
                })
            for suffix, tenant, confidence in (
                    ("review", "weak-board", 0.45),
                    ("exact-mismatch", "different-board", 0.97)):
                upsert(connection, m.company_ats_tenants, {
                    "id": f"browser-ats-{suffix}", "company_id": f"browser-company-{suffix}",
                    "provider": "greenhouse", "tenant_identifier": tenant,
                    "organization_identifier": "", "board_identifier": "",
                    "confidence": confidence,
                    "evidence": ["SYNTHETIC_STORED_EVIDENCE"],
                    "first_seen_at": "2026-10-08T00:00:00Z",
                    "last_seen_at": "2026-10-08T00:00:00Z",
                })
            company_id = "browser-company-no-route"
            upsert(connection, m.companies, {"id": company_id, "name": "F No route", "domain": ""})
            upsert(connection, m.jobs, {
                "id": "browser-job-no-route", "company_id": company_id,
                "requisition": "no-route", "title": "F No route",
                "description": "Synthetic browser acceptance fixture.", "skills": [],
                "location": "Remote", "remote_type": "REMOTE",
            })


if __name__ == "__main__":
    unittest.main()
