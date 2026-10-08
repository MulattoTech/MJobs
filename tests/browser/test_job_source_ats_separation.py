"""Connected browser acceptance for discovery-source and ATS separation."""
from pathlib import Path
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
from packages.persistence import models as m
from packages.persistence.store import Store, now, upsert
from services.company_discovery.sources import register_ats_tenant
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class JobSourceAtsSeparationBrowserTests(unittest.TestCase):
    def test_source_and_route_provider_stay_distinct_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-source-ats-browser-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'browser.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))

            fixtures = (
                {
                    "id": "browser-source-ats-mock-route",
                    "company_id": "browser-source-ats-mock-company",
                    "company": "Route Provider Example",
                    "domain": "route-provider.example.test",
                    "title": "Unverified mock route",
                    "provider": "mock-ats",
                    "tenant": "unverified-board",
                },
                {
                    "id": "browser-source-ats-verified-route",
                    "company_id": "browser-source-ats-verified-company",
                    "company": "Verified ATS Example",
                    "domain": "verified-ats.example.test",
                    "title": "Evidence-qualified Greenhouse route",
                    "provider": "greenhouse",
                    "tenant": "verified-board",
                },
            )
            with store.engine.begin() as connection:
                for fixture in fixtures:
                    upsert(connection, m.companies, {
                        "id": fixture["company_id"], "name": fixture["company"],
                        "domain": fixture["domain"],
                    })
                    upsert(connection, m.jobs, {
                        "id": fixture["id"], "company_id": fixture["company_id"],
                        "requisition": fixture["id"], "title": fixture["title"],
                        "description": "Synthetic browser fixture; no application is submitted.",
                        "skills": [], "location": "Remote", "remote_type": "REMOTE",
                        "attributes": {"fictional": True},
                    })
                    upsert(connection, m.routes, {
                        "id": "route-" + fixture["id"], "job_id": fixture["id"],
                        "company_id": fixture["company_id"], "provider": fixture["provider"],
                        "tenant": fixture["tenant"], "requisition": fixture["id"],
                        "url": f"https://jobs.example.test/apply/{fixture['id']}",
                    })
                    upsert(connection, m.postings, {
                        "id": "posting-" + fixture["id"], "job_id": fixture["id"],
                        "source": "fixture-board", "source_job_id": fixture["id"],
                        "source_url": f"https://board.example.test/jobs/{fixture['id']}",
                        "application_url": f"https://jobs.example.test/apply/{fixture['id']}",
                        "raw": {"fixture": True}, "fetched_at": now(),
                    })

            register_ats_tenant(
                store, "browser-source-ats-verified-company", provider="greenhouse",
                tenant_identifier="verified-board", confidence=0.95,
                evidence=["synthetic browser acceptance evidence"],
            )
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
                        self.fail("Isolated source/ATS API did not start")
                    time.sleep(0.02)

                external = []
                api_requests = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            api_requests.append((route.request.method, parsed.path, parse_qs(parsed.query)))
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")

                    route_card = page.locator(".job-card").filter(has_text="Unverified mock route")
                    expect(route_card).to_be_visible()
                    expect(route_card.get_by_text("DISCOVERY SOURCE", exact=True)).to_be_visible()
                    expect(route_card.get_by_text("fixture-board", exact=True)).to_be_visible()
                    expect(route_card.get_by_text("APPLICATION PROVIDER", exact=True)).to_be_visible()
                    expect(route_card.get_by_text("mock-ats", exact=True)).to_be_visible()
                    expect(route_card.get_by_text("VERIFIED ATS", exact=True)).to_have_count(0)

                    # A route-provider value alone does not qualify a job for the ATS facet.
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "ats_provider=mock-ats" in response.url) as unverified_ats:
                        page.get_by_label("ATS", exact=True).select_option("mock-ats")
                    self.assertEqual(unverified_ats.value.status, 200)
                    self.assertNotIn("browser-source-ats-mock-route",
                                     {job["id"] for job in unverified_ats.value.json()["jobs"]})

                    # Persisted evidence still qualifies the separate Greenhouse ATS facet.
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "ats_provider=greenhouse" in response.url) as verified_ats:
                        page.get_by_label("ATS", exact=True).select_option("greenhouse")
                    self.assertEqual([job["id"] for job in verified_ats.value.json()["jobs"]],
                                     ["browser-source-ats-verified-route"])

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "ats_provider=" not in response.url) as ats_cleared:
                        page.get_by_label("ATS", exact=True).select_option("")
                    self.assertEqual(ats_cleared.value.status, 200)

                    # The existing Source filter continues to filter route.provider and
                    # keeps its persisted search-profile key/value across reload.
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and "provider=mock-ats" in response.url) as mock_route:
                        page.get_by_label("Application provider", exact=True).select_option("mock-ats")
                    self.assertEqual([job["id"] for job in mock_route.value.json()["jobs"]],
                                     ["browser-source-ats-mock-route"])
                    page.get_by_label("Saved search name", exact=True).fill("Mock route provider")
                    page.get_by_role("button", name="Save current filters", exact=True).click()
                    saved = page.get_by_label("Saved search profile", exact=True)
                    saved_id = saved.locator("option", has_text="Mock route provider").get_attribute("value")
                    self.assertTrue(saved_id)

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    saved = page.get_by_label("Saved search profile", exact=True)
                    expect(saved.locator("option", has_text="Mock route provider")).to_have_count(1)
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + "/jobs?")
                            and f"search_profile_id={saved_id}" in response.url
                            and "provider=mock-ats" in response.url) as restored:
                        saved.select_option(saved_id)
                    self.assertEqual([job["id"] for job in restored.value.json()["jobs"]],
                                     ["browser-source-ats-mock-route"])
                    expect(page.get_by_label("Application provider", exact=True)).to_have_value("mock-ats")
                    expect(page.get_by_label("ATS", exact=True)).to_have_value("")
                    restored_card = page.locator(".job-card").filter(has_text="Unverified mock route")
                    expect(restored_card.get_by_text("DISCOVERY SOURCE", exact=True)).to_be_visible()
                    expect(restored_card.get_by_text("fixture-board", exact=True)).to_be_visible()
                    expect(restored_card.get_by_text("APPLICATION PROVIDER", exact=True)).to_be_visible()
                    expect(restored_card.get_by_text("mock-ats", exact=True)).to_be_visible()
                    saved_response = page.request.get(origin + "/search-profiles?profile_id=fixture")
                    self.assertEqual(saved_response.status, 200)
                    saved_profile = next(row for row in saved_response.json()["search_profiles"]
                                         if row["id"] == saved_id)
                    self.assertEqual(saved_profile["criteria"]["job_filters"]["provider"], "mock-ats")
                    self.assertEqual(saved_profile["criteria"]["job_filters"]["ats_provider"], "")

                    self.assertEqual(external, [])
                    self.assertFalse(any(
                        method != "GET" and (path.startswith("/sources") or path.startswith("/collect")
                                              or path.startswith("/discovery"))
                        for method, path, _ in api_requests), api_requests)
                    self.assertFalse(any(method != "GET" and path.startswith("/applications")
                                         for method, path, _ in api_requests), api_requests)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated source/ATS API did not stop")
            reopened = Store(database_url)
            try:
                self.assertEqual(reopened.count("worker_operations"), operations_before)
                self.assertEqual(reopened.count("applications"), applications_before)
            finally:
                reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
