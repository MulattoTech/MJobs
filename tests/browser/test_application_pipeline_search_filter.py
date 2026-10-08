"""Chromium acceptance for the connected, user-reported pipeline status facet."""
# Expected first red on the unmodified base: the connected Jobs page has no
# accessible user-reported pipeline status control or saved-filter wiring.
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
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.tracking.applications import PIPELINE_STATUSES, set_application_pipeline_status
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationPipelineSearchFilterBrowserTests(unittest.TestCase):
    def test_status_filter_is_visible_persisted_profile_scoped_and_has_no_application_side_effects(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-pipeline-status-browser-") as directory:
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
            jobs = {row["title"]: row for row in store.jobs("fixture")}
            self.assertIn("Python Developer", jobs)
            self.assertIn("Data Engineer", jobs)
            python_application = prepare(store, jobs["Python Developer"]["id"], "fixture", root)
            prepare(store, jobs["Data Engineer"]["id"], "fixture", root)
            set_application_pipeline_status(store, "fixture", python_application,
                                            "APPLIED", "browser-status-applied")

            operations_before = store.count("worker_operations")
            applications_before = store.count("applications")
            attempts_before = store.count("attempts")
            with store.engine.connect() as connection:
                job_ids_before = connection.execute(select(m.jobs.c.id).order_by(m.jobs.c.id)).all()
                events_before = connection.execute(select(m.audit.c.id).where(
                    m.audit.c.action == "APPLICATION_PIPELINE_STATUS")).all()

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated application status search API did not start")
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

                    status_filter = page.get_by_label(
                        re.compile(r"application pipeline status.*reported by you", re.IGNORECASE))
                    expect(status_filter).to_be_visible()
                    status_options = status_filter.locator("option").evaluate_all(
                        "options => options.map(option => option.value).filter(Boolean)")
                    self.assertEqual(set(status_options), PIPELINE_STATUSES | {"NOT_TRACKED"})
                    expect(status_filter.locator("option[value='APPLIED']")).to_have_count(1)
                    expect(status_filter.locator("option[value='NOT_TRACKED']")).to_have_count(1)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_pipeline_status=APPLIED" in response.url) as applied_response:
                        status_filter.select_option("APPLIED")
                    self.assertEqual(applied_response.value.status, 200)
                    self.assertEqual([job["title"] for job in applied_response.value.json()["jobs"]],
                                     ["Python Developer"])
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_have_count(0)

                    page.get_by_label("Saved search name", exact=True).fill("Applied roles")
                    page.get_by_role("button", name="Save current filters", exact=True).click()
                    saved = page.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Applied roles")).to_have_count(1)
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    saved = page.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Applied roles")).to_have_count(1)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "search_profile_id=" in response.url
                            and "application_pipeline_status=APPLIED" in response.url) as restored:
                        saved.select_option(saved.locator("option", has_text="Applied roles").get_attribute("value"))
                    self.assertEqual(restored.value.status, 200)
                    self.assertEqual([job["title"] for job in restored.value.json()["jobs"]],
                                     ["Python Developer"])
                    expect(status_filter).to_have_value("APPLIED")

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_pipeline_status=NOT_TRACKED" in response.url) as untracked:
                        status_filter.select_option("NOT_TRACKED")
                    self.assertEqual(untracked.value.status, 200)
                    self.assertEqual([job["title"] for job in untracked.value.json()["jobs"]],
                                     ["Data Engineer"])
                    expect(page.locator(".job-card").filter(has_text="Data Engineer")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="Python Developer")).to_have_count(0)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_pipeline_status=APPLIED" not in response.url
                            and "application_pipeline_status=NOT_TRACKED" not in response.url) as cleared:
                        status_filter.select_option("")
                    self.assertEqual(cleared.value.status, 200)
                    self.assertEqual(len(cleared.value.json()["jobs"]), 2)
                    expect(status_filter).to_have_value("")

                    page.get_by_label("Active profile", exact=True).select_option("other")
                    isolated_search = page.get_by_label("Saved search profile", exact=True)
                    expect(isolated_search).to_have_value("")
                    expect(isolated_search.locator("option", has_text="Applied roles")).to_have_count(0)
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()

                self.assertEqual(store.count("worker_operations"), operations_before)
                self.assertEqual(store.count("applications"), applications_before)
                self.assertEqual(store.count("attempts"), attempts_before)
                self.assertIsNone(store.application(python_application)["confirmation"])
                with store.engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.jobs.c.id).order_by(m.jobs.c.id)).all(),
                                     job_ids_before)
                    self.assertEqual(connection.execute(select(m.audit.c.id).where(
                        m.audit.c.action == "APPLICATION_PIPELINE_STATUS")).all(), events_before)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
