"""Connected browser proof for profile route preference and explicit route selection."""
from pathlib import Path
from fixtures.browser_ports import bound_browser_socket
import copy
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import insert
import uvicorn

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class ApplicationRoutePreferenceBrowserTests(unittest.TestCase):
    def test_profile_choice_requires_selecting_a_route_before_preparation(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-route-preference-browser-") as directory:
            store = Store(f"sqlite:///{(Path(directory) / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(copy.deepcopy(PROFILE))
            job_id = "b" * 64
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id":"browser-route-company", "name":"Route Choice Employer", "domain":"routechoice.example",
                })
                upsert(connection, m.jobs, {
                    "id":job_id, "company_id":"browser-route-company", "requisition":"route-choice",
                    "title":"Route Selection Engineer", "description":"Support local systems.",
                    "skills":["Python"], "location":"Remote", "remote_type":"REMOTE", "attributes":{},
                })
                upsert(connection, m.routes, {
                    "id":"browser-route-board", "job_id":job_id, "company_id":"browser-route-company",
                    "provider":"job-board", "tenant":"integrated-board", "requisition":"route-choice",
                    "url":"https://board.example/jobs/route-choice",
                })
                upsert(connection, m.routes, {
                    "id":"browser-route-ats", "job_id":job_id, "company_id":"browser-route-company",
                    "provider":"greenhouse", "tenant":"routechoice", "requisition":"route-choice",
                    "url":"https://job-boards.greenhouse.io/routechoice/jobs/1",
                })
                upsert(connection, m.company_ats_tenants, {
                    "id":"browser-route-verified-ats", "company_id":"browser-route-company",
                    "provider":"greenhouse", "tenant_identifier":"routechoice",
                    "organization_identifier":"", "board_identifier":"", "confidence":0.94,
                    "evidence":["PUBLIC_API_BOARD"], "first_seen_at":"2026-10-09T00:00:00Z",
                    "last_seen_at":"2026-10-09T00:00:00Z",
                })
                connection.execute(insert(m.postings).values(
                    id="browser-route-posting", job_id=job_id, source="job-board",
                    source_job_id="route-choice", source_url="https://board.example/jobs/route-choice",
                    application_url="https://board.example/jobs/route-choice", raw={},
                    fetched_at="2026-10-09T00:00:00Z",
                ))
            app = create_app(store, "http://127.0.0.1:1", ROOT,
                             "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets":[sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated route-preference API did not start")
                    time.sleep(0.02)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    external, errors, application_requests, route_checks = [], [], [], []

                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                            return
                        if (parsed.path == f"/jobs/{job_id}/routes/browser-route-board/resolve"
                                and route.request.method == "POST"):
                            route_checks.append(route.request.post_data)
                            route.fulfill(status=200, json={
                                "source_route_id":"browser-route-board",
                                "status":"RESOLVED_ATS_ROUTE",
                                "candidate_url":"https://boards.greenhouse.io/routechoice/jobs/1",
                                "provider":"greenhouse", "tenant":"routechoice",
                            })
                            return
                        if parsed.path == "/applications" and route.request.method == "POST":
                            application_requests.append(route.request.post_data_json)
                            route.fulfill(status=202, json={"operation_id":"route-preference-operation","state":"QUEUED"})
                            return
                        if parsed.path == "/operations/route-preference-operation":
                            route.fulfill(status=200, json={
                                "id":"route-preference-operation", "kind":"PREPARE", "entity_id":PROFILE["id"],
                                "params":{}, "state":"COMPLETED", "error":None,
                                "attempt_count":1, "created_at":"2026-10-09T00:00:00Z",
                                "updated_at":"2026-10-09T00:00:00Z",
                            })
                            return
                        route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option(str(PROFILE["id"]))
                    page.get_by_role("button", name="Profile", exact=True).click()
                    page.get_by_role("radio", name="Ask me to choose a route for each job").check()
                    with page.expect_response(lambda response: response.request.method == "PUT"
                                               and urlsplit(response.url).path == "/profiles"):
                        page.get_by_role("button", name="Save profile", exact=True).first.click()
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Profile", exact=True).click()
                    expect(page.get_by_role("radio", name="Ask me to choose a route for each job")).to_be_checked()
                    page.get_by_role("button", name=re.compile(r"^Jobs(?:\s|$)")).click()
                    page.get_by_role("button", name=re.compile("Route Selection Engineer")).click()
                    route_picker = page.get_by_label("Choose the application route for this job")
                    expect(route_picker).to_be_visible()
                    expect(route_picker.locator("option").nth(1)).to_contain_text(
                        "greenhouse · routechoice · verified identity evidence")
                    expect(route_picker.locator("option").nth(2)).to_contain_text(
                        "job-board · integrated-board · route not verified")
                    expect(route_picker.locator("option").nth(2)).to_contain_text(
                        "same employer requisition has verified ATS route: greenhouse · routechoice")
                    prepare = page.get_by_role("button", name="Prepare documents", exact=True)
                    expect(prepare).to_be_disabled()
                    route_picker.select_option("browser-route-board")
                    expect(prepare).to_be_enabled()
                    check_route = page.get_by_role("button", name="Check employer route", exact=True)
                    with page.expect_response(lambda response: response.request.method == "POST"
                                              and response.url.endswith("/jobs/" + job_id + "/routes/browser-route-board/resolve")):
                        check_route.click()
                    expect(page.get_by_role("link", name="Review detected ATS destination ↗")).to_have_attribute(
                        "href", "https://boards.greenhouse.io/routechoice/jobs/1")
                    expect(prepare).to_be_enabled()
                    prepare.click()
                    expect(page.get_by_text("Completed. Your workspace has been updated.", exact=False)).to_be_visible()
                    self.assertEqual(len(application_requests), 1)
                    self.assertEqual(application_requests[0]["route_mode"], "THIS_ROUTE")
                    self.assertEqual(application_requests[0]["route_id"], "browser-route-board")
                    self.assertEqual(route_checks, [None])
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated route-preference API must stop")
