"""Connected browser acceptance for saved application-link availability."""
from pathlib import Path
import os
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE

ROOT = Path(os.environ.get("MJOBS_BROWSER_APP_ROOT", Path(__file__).resolve().parents[2]))


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationLinkFilterBrowserTests(unittest.TestCase):
    def test_accessible_filter_connects_and_saved_value_survives_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-application-link-browser-") as directory:
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

            with store.engine.connect() as connection:
                company_id = connection.execute(select(m.jobs.c.company_id).limit(1)).scalar_one()
            fixtures = (
                ("available", "Z Available Link", {"application_url_status": "PROVIDER_SUPPLIED"}),
                ("unavailable", "Y Unavailable Link", {"application_url_status": "UNAVAILABLE"}),
                ("unknown", "A Unknown Link", {"application_url_status": "INFERRED"}),
                ("missing", "B Missing Link", {}),
            )
            with store.engine.begin() as connection:
                for suffix, title, attributes in fixtures:
                    job_id = f"browser-application-link-{suffix}"
                    upsert(connection, m.jobs, {
                        "id": job_id, "company_id": company_id, "requisition": job_id,
                        "title": title, "description": "Synthetic browser fixture.",
                        "skills": [], "location": "Remote", "remote_type": "REMOTE",
                        "attributes": attributes,
                    })
                    upsert(connection, m.routes, {
                        "id": f"route-{job_id}", "job_id": job_id, "company_id": company_id,
                        "provider": "fixture", "tenant": "application-link-browser",
                        "requisition": job_id,
                        "url": f"https://jobs.example.test/apply/{job_id}",
                    })
            operations_before = store.count("worker_operations")
            applications_before = store.count("applications")
            store.engine.dispose()
            store = Store(database_url)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
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
                        self.fail("Isolated application-link API did not start")
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
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    availability = page.get_by_label("Application link", exact=True)
                    expect(availability.locator("option")).to_have_count(3)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_link_status=AVAILABLE" in response.url) as available:
                        availability.select_option("AVAILABLE")
                    self.assertEqual([row["id"] for row in available.value.json()["jobs"]],
                                     ["browser-application-link-available"])
                    expect(page.locator(".job-card").filter(has_text="Z Available Link")).to_be_visible()
                    expect(page.locator(".job-card").filter(has_text="A Unknown Link")).to_have_count(0)
                    expect(page.locator(".job-card").filter(has_text="B Missing Link")).to_have_count(0)

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_link_status=UNAVAILABLE" in response.url) as unavailable:
                        availability.select_option("UNAVAILABLE")
                    self.assertEqual([row["id"] for row in unavailable.value.json()["jobs"]],
                                     ["browser-application-link-unavailable"])

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "application_link_status=" not in response.url) as cleared:
                        availability.select_option("")
                    self.assertEqual(cleared.value.status, 200)
                    self.assertEqual({row["id"] for row in cleared.value.json()["jobs"]}
                                     & {"browser-application-link-unknown", "browser-application-link-missing"},
                                     {"browser-application-link-unknown", "browser-application-link-missing"})

                    page.get_by_label("Saved search name", exact=True).fill("Available application links")
                    availability.select_option("AVAILABLE")
                    page.get_by_role("button", name="Save current filters", exact=True).click()
                    saved = page.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Available application links")).to_have_count(1)

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    saved = page.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Available application links")).to_have_count(1)
                    selected_value = saved.locator(
                        "option", has_text="Available application links").get_attribute("value")
                    self.assertTrue(selected_value, "Saved search option must expose its persisted ID")
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and f"search_profile_id={selected_value}" in response.url
                            and "application_link_status=AVAILABLE" in response.url) as restored:
                        saved.select_option(selected_value)
                    self.assertEqual([row["id"] for row in restored.value.json()["jobs"]],
                                     ["browser-application-link-available"])
                    expect(page.get_by_label("Application link", exact=True)).to_have_value("AVAILABLE")

                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated application-link API did not stop")
            reopened = Store(database_url)
            try:
                self.assertEqual(reopened.count("worker_operations"), operations_before)
                self.assertEqual(reopened.count("applications"), applications_before)
            finally:
                reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
