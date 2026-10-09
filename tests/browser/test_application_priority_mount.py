"""Connected acceptance for the mounted, scoped application-priority panel."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
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
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
PRIORITY_ROUTE = "/profiles/{profile_id}/jobs/{job_id}/application-priority"


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationPriorityMountedPanelBrowserTests(unittest.TestCase):
    @staticmethod
    def _snapshot(store):
        with store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def test_mounted_panel_hides_misscoped_data_and_retries_read_only(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-priority-mounted-panel-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'priority.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")

            primary = copy.deepcopy(PROFILE)
            primary["preferences"] = {
                **primary.get("preferences", {}),
                "salary_expectations": "USD 90000-110000 annually",
            }
            other = copy.deepcopy(PROFILE)
            other.update(
                id="priority-panel-other",
                name="Synthetic Other Priority Profile",
                email="priority-panel-other@example.test",
                facts=[],
            )
            store.save_profile(primary)
            store.save_profile(other)

            known_job = capture_manual_job(store, {
                "request_id": "priority-panel-known",
                "title": "Synthetic Priority Panel Known Role",
                "company": "Synthetic Priority Panel Employer",
                "description": "Fictional job with supported skills and a stated salary range.",
                "reference_url": "https://jobs.example.test/priority-panel-known",
                "profile_id": primary["id"],
            })["job"]
            partial_job = capture_manual_job(store, {
                "request_id": "priority-panel-partial",
                "title": "Synthetic Priority Panel Unknown Role",
                "company": "Synthetic Priority Panel Employer",
                "description": "Fictional job with incomplete salary context.",
                "reference_url": "https://jobs.example.test/priority-panel-partial",
                "profile_id": primary["id"],
            })["job"]

            observed = datetime.now(timezone.utc)
            with store.engine.begin() as connection:
                connection.execute(m.jobs.update().where(m.jobs.c.id == known_job["id"]).values(
                    attributes={
                        **known_job["attributes"],
                        "salary_min": 100000,
                        "salary_max": 120000,
                        "salary_currency": "USD",
                        "salary_period": "YEAR",
                        "salary_source": "SOURCE_PROVIDED",
                        "reported_posted_at": (observed - timedelta(days=2)).isoformat(),
                        "expires_at": (observed + timedelta(days=7)).isoformat(),
                    },
                ))
                connection.execute(m.jobs.update().where(m.jobs.c.id == partial_job["id"]).values(
                    attributes={
                        **partial_job["attributes"],
                        "salary_min": 100000,
                        "salary_max": None,
                        "salary_currency": "USD",
                        "salary_period": "YEAR",
                        "salary_source": "SOURCE_PROVIDED",
                    },
                ))

            recalculate(store, primary["id"])
            recalculate(store, other["id"])
            expected_match_scores = {
                (profile_id, job_id): store.job(job_id, profile_id)["match"]["score"]
                for profile_id in (primary["id"], other["id"])
                for job_id in (known_job["id"], partial_job["id"])
            }
            before_browser = self._snapshot(store)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                create_app(store, "http://127.0.0.1:1", root,
                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"),
                log_level="error", timeout_graceful_shutdown=2))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated priority-panel API did not start")
                    time.sleep(0.02)

                external = []
                requests = []
                fail_known_once = True
                misbind_job_once = True
                misbind_profile_once = True

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1365, "height": 1000})

                    def local_only(route):
                        nonlocal fail_known_once, misbind_job_once, misbind_profile_once
                        parsed = urlsplit(route.request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                            return

                        requests.append((route.request.method, parsed.path))
                        segments = parsed.path.strip("/").split("/")
                        is_priority_read = (
                            route.request.method == "GET" and len(segments) == 5
                            and segments[0] == "profiles" and segments[2] == "jobs"
                            and segments[4] == "application-priority"
                        )
                        if is_priority_read:
                            profile_id, job_id = segments[1], segments[3]
                            if job_id == known_job["id"] and fail_known_once:
                                fail_known_once = False
                                route.fulfill(status=503, content_type="application/json",
                                              body=json.dumps({"detail": "synthetic transient failure"}))
                                return

                            response = route.fetch()
                            body = response.json()
                            if job_id == partial_job["id"] and misbind_job_once:
                                misbind_job_once = False
                                body["job_id"] = "different-synthetic-job"
                                route.fulfill(status=response.status,
                                              content_type="application/json", body=json.dumps(body))
                                return
                            if profile_id == other["id"] and job_id == known_job["id"] and misbind_profile_once:
                                misbind_profile_once = False
                                body["profile_id"] = primary["id"]
                                route.fulfill(status=response.status,
                                              content_type="application/json", body=json.dumps(body))
                                return
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    known_card = page.locator(".job-card").filter(
                        has_text="Synthetic Priority Panel Known Role")
                    partial_card = page.locator(".job-card").filter(
                        has_text="Synthetic Priority Panel Unknown Role")
                    expect(known_card).to_be_visible()
                    expect(partial_card).to_be_visible()

                    known_card.click()
                    panel = page.get_by_role(
                        "region", name="Application priority explanation", exact=True)
                    expect(panel).to_be_visible()
                    expect(panel.get_by_role("alert")).to_contain_text(
                        "Application priority could not be loaded")
                    retry = panel.get_by_role("button", name="Retry", exact=True)
                    expect(retry).to_be_enabled()
                    with page.expect_response(lambda response:
                            response.url.endswith(PRIORITY_ROUTE.format(
                                profile_id="fixture", job_id=known_job["id"]))
                            and response.request.method == "GET") as retry_response:
                        retry.click()
                    self.assertEqual(retry_response.value.status, 200)
                    known_result = retry_response.value.json()
                    self.assertEqual(known_result["profile_id"], "fixture")
                    self.assertEqual(known_result["job_id"], known_job["id"])
                    self.assertEqual(known_result["match_score"],
                                     expected_match_scores[("fixture", known_job["id"])])
                    self.assertTrue(known_result["advisory"])
                    self.assertFalse(known_result["affects_match_score"])
                    expect(panel).to_contain_text("Priority band")
                    expect(panel).to_contain_text("Match score")
                    factors = panel.get_by_role("list", name="Priority factors", exact=True)
                    expect(factors).to_be_visible()
                    known_factor = next(row for row in known_result["factors"]
                                        if row["status"] == "KNOWN")
                    unknown_factor = next(row for row in known_result["factors"]
                                          if row["status"] == "UNKNOWN")
                    expect(factors).to_contain_text(known_factor["label"])
                    expect(factors).to_contain_text(known_factor["explanation"])
                    expect(factors).to_contain_text(unknown_factor["label"])
                    expect(factors).to_contain_text("Status: UNKNOWN")
                    expect(factors).to_contain_text(unknown_factor["explanation"])
                    expect(factors).to_contain_text("Priority contribution: UNKNOWN")

                    # A server response for another job must never expose its score or factors.
                    partial_card.click()
                    panel = page.get_by_role(
                        "region", name="Application priority explanation", exact=True)
                    expect(panel).to_have_count(1)
                    expect(panel.get_by_role("alert")).to_contain_text(
                        "does not match the selected profile and job")
                    expect(panel.get_by_role("list", name="Priority factors", exact=True)).to_have_count(0)
                    expect(panel.locator("dl")).to_have_count(0)
                    expect(panel).not_to_contain_text("Priority contribution:")

                    # The API's correctly scoped row for the second profile is deliberately
                    # mislabeled at the browser boundary; the component must hide it likewise.
                    page.get_by_label("Active profile", exact=True).select_option(other["id"])
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value(other["id"])
                    known_card = page.locator(".job-card").filter(
                        has_text="Synthetic Priority Panel Known Role")
                    known_card.click()
                    panel = page.get_by_role(
                        "region", name="Application priority explanation", exact=True)
                    expect(panel.get_by_role("alert")).to_contain_text(
                        "does not match the selected profile and job")
                    expect(panel.get_by_role("list", name="Priority factors", exact=True)).to_have_count(0)
                    expect(panel.locator("dl")).to_have_count(0)

                    # Selecting the primary profile and job again loads the real scoped
                    # explanation after the deliberately misbound response has been consumed.
                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    known_card = page.locator(".job-card").filter(
                        has_text="Synthetic Priority Panel Known Role")
                    known_card.click()
                    panel = page.get_by_role(
                        "region", name="Application priority explanation", exact=True)
                    expect(panel.get_by_role("list", name="Priority factors", exact=True)).to_be_visible()
                    expect(panel).to_contain_text("Application priority is advisory only.")

                    self.assertFalse(fail_known_once)
                    self.assertFalse(misbind_job_once)
                    self.assertFalse(misbind_profile_once)
                    self.assertEqual(external, [])
                    self.assertFalse([request for request in requests if request[0] != "GET"], requests)
                    self.assertFalse(any(method in {"POST", "PUT", "PATCH", "DELETE"}
                                          and path.startswith("/applications")
                                          for method, path in requests),
                                     [row for row in requests if row[0] in {"POST", "PUT", "PATCH", "DELETE"}
                                      and row[1].startswith("/applications")])
                    self.assertFalse(any(method in {"POST", "PUT", "PATCH", "DELETE"}
                                          and path.startswith(("/discover", "/sources", "/collect"))
                                          for method, path in requests),
                                     [row for row in requests if row[0] in {"POST", "PUT", "PATCH", "DELETE"}
                                      and row[1].startswith(("/discover", "/sources", "/collect"))])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated priority-panel API must stop")

            reopened = Store(database_url)
            try:
                self.assertEqual(self._snapshot(reopened), before_browser)
            finally:
                reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
