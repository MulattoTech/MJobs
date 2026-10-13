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
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import insert
import uvicorn

from apps.api.main import create_app
from services.company_discovery.sources import upsert_company_job_source
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.discovery.sources.collect import configure_source
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
            board_source_id = configure_source(store, "public", provider="remotive")
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id":"browser-route-company", "name":"Route Choice Employer", "domain":"routechoice.example",
                })
                upsert(connection, m.jobs, {
                    "id":job_id, "company_id":"browser-route-company", "requisition":"route-choice",
                    "title":"Route Selection Engineer", "description":"Support local systems.",
                    "skills":["Python"], "location":"Remote", "remote_type":"REMOTE",
                    "attributes":{"source_id":board_source_id,"provider":"remotive","tenant":"public"},
                })
                upsert(connection, m.routes, {
                    "id":"browser-route-board", "job_id":job_id, "company_id":"browser-route-company",
                    "provider":"job-board", "tenant":"integrated-board", "requisition":"route-choice",
                    "url":"https://board.example/jobs/route-choice",
                })
                upsert(connection, m.routes, {
                    "id":"browser-route-remotive", "job_id":job_id, "company_id":"browser-route-company",
                    "provider":"remotive", "tenant":"public", "requisition":"route-choice",
                    "url":"https://remotive.com/remote-jobs/example/1",
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
            board_source = upsert_company_job_source(
                store, company_id="browser-route-company", provider="remotive",
                tenant_identifier="public", public_api_url="https://remotive.com/api/remote-jobs",
                collection_strategy="PUBLIC_JSON",
            )
            with store.engine.begin() as connection:
                connection.execute(
                    m.company_sources.update().where(m.company_sources.c.id == board_source["id"])
                    .values(collector_source_id=board_source_id)
                )
                connection.execute(insert(m.postings).values(
                    id="browser-route-remotive-posting", job_id=job_id,
                    source="https://remotive.com/api/remote-jobs",
                    source_job_id="route-choice-board-copy",
                    source_url="https://remotive.com/api/remote-jobs",
                    application_url="https://remotive.com/remote-jobs/example/1", raw={},
                    fetched_at="2026-10-09T00:00:00Z",
                ))
            app = create_app(store, "http://127.0.0.1:1", ROOT,
                             "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
            route_resolution = patch(
                "apps.api.main.resolve_aggregator_redirect",
                return_value={
                    "status": "RESOLVED_ATS_ROUTE",
                    "candidate_url": "https://boards.greenhouse.io/routechoice/jobs/1",
                    "provider": "greenhouse", "tenant": "routechoice",
                },
            )
            resolver = route_resolution.start()
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
                    external, errors, application_requests, route_checks, route_saves = [], [], [], [], []

                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                            return
                        if (parsed.path == f"/jobs/{job_id}/routes/browser-route-board/resolve"
                                and route.request.method == "POST"):
                            route_checks.append(route.request.post_data)
                            route.continue_()
                            return
                        if (parsed.path.endswith("/save-verified-destination")
                                and route.request.method == "POST"):
                            route_saves.append(route.request.post_data)
                            route.continue_()
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
                    expect(route_picker.locator("option").nth(3)).to_contain_text(
                        "remotive · public · trusted integrated board evidence")
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
                    # Rechecking must reject a changed employer URL even if
                    # stale resolution metadata still names the verified tenant.
                    verified_result = dict(resolver.return_value)
                    resolver.return_value = {**verified_result,
                        'candidate_url': 'https://boards.greenhouse.io/other/jobs/77'}
                    with page.expect_response(lambda response: response.request.method == 'POST'
                            and response.url.endswith('/save-verified-destination')) as rejected:
                        page.get_by_role('button', name='Save detected destination as selectable route',
                                         exact=True).click()
                    self.assertEqual(rejected.value.status, 409)
                    expect(page.get_by_role('alert').filter(
                        has_text='The destination could not be saved.')).to_be_visible()
                    expect(route_picker).to_have_value('browser-route-board')
                    self.assertEqual(store.count('application_routes'), 3)
                    self.assertEqual(application_requests, [])
                    resolver.return_value = verified_result
                    with page.expect_response(lambda response: response.request.method == "POST"
                                               and response.url.endswith(
                                                   "/jobs/" + job_id + "/routes/browser-route-board/"
                                                   "save-verified-destination")) as saved_response:
                        page.get_by_role("button", name="Save detected destination as selectable route",
                                         exact=True).click()
                    saved_route = saved_response.value.json()["route"]
                    saved_route_id = saved_route["id"]
                    expect(page.get_by_text(
                        "Saved as a selectable route. Your current route selection is unchanged. Review the option above; preparation still follows your saved route preference. Nothing is submitted.",
                        exact=True,
                    )).to_be_visible()
                    expect(route_picker).to_have_value("browser-route-board")
                    expect(route_picker.locator(f"option[value='{saved_route_id}']")).to_contain_text(
                        "greenhouse · routechoice · verified identity evidence",
                    )
                    with page.expect_response(lambda response: response.request.method == "PUT"
                                               and urlsplit(response.url).path == (
                                                   f"/profiles/{PROFILE['id']}/companies/browser-route-company/"
                                                   "preferred-application-route")):
                        page.get_by_role("button", name="Prefer this route for this employer", exact=True).click()
                    expect(page.get_by_role("button", name="Clear employer route preference", exact=True)).to_be_visible()
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name=re.compile(r"^Jobs(?:\s|$)")).click()
                    page.get_by_role("button", name=re.compile("Route Selection Engineer")).click()
                    route_picker = page.get_by_label("Choose the application route for this job")
                    expect(route_picker).to_have_value("browser-route-board")
                    expect(route_picker.locator(f"option[value='{saved_route_id}']")).to_be_attached()
                    expect(page.get_by_role("button", name="Clear employer route preference", exact=True)).to_be_visible()
                    prepare = page.get_by_role("button", name="Prepare documents", exact=True)
                    route_picker.select_option(saved_route_id)
                    expect(prepare).to_be_enabled()
                    prepare.click()
                    expect(page.get_by_text("Completed. Your workspace has been updated.", exact=False)).to_be_visible()
                    self.assertEqual(len(application_requests), 1)
                    self.assertEqual(application_requests[0]["route_mode"], "THIS_ROUTE")
                    self.assertEqual(application_requests[0]["route_id"], saved_route_id)
                    self.assertEqual(route_checks, [None])
                    self.assertEqual(route_saves, [None, None])
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
                route_resolution.stop()
                self.assertFalse(thread.is_alive(), "Isolated route-preference API must stop")
