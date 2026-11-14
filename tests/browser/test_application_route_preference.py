"""Connected browser proof for profile route preference and explicit route selection."""
from pathlib import Path
from fixtures.browser_ports import bound_browser_socket
import asyncio
from concurrent.futures import Future
import copy
import hashlib
import json
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import func, insert, select
import pytest
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


class _RouteSaveResponseGate:
    """Pause one real response body after the endpoint has committed, without blocking ASGI."""
    def __init__(self, app, path):
        self.app = app
        self.path = path
        self.entered = threading.Event()
        self.sent = threading.Event()
        self.release_signal = Future()
        self.target_requests = 0
        self.status = None
        self.receipt = None
        self.body_sha256 = None
        self.timed_out = False
        self.release_requested = False
        self.released_by_test = False

    def release(self):
        self.release_requested = True
        if not self.release_signal.done():
            self.release_signal.set_result(None)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != self.path:
            await self.app(scope, receive, send)
            return
        self.target_requests += 1
        if self.target_requests != 1:
            await self.app(scope, receive, send)
            return
        body = bytearray()

        async def held_send(message):
            if message["type"] == "http.response.start":
                self.status = message["status"]
            if message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                assert len(body) <= 65536, "Route save fixture response exceeded its bound"
                if not message.get("more_body", False):
                    result = json.loads(body)
                    route = result.get("route") or {}
                    self.receipt = {
                        "source_route_id": result.get("source_route_id"),
                        "status": result.get("status"),
                        "created": result.get("created"),
                        "route_id": route.get("id"),
                        "route_url": route.get("url"),
                    }
                    self.body_sha256 = hashlib.sha256(body).hexdigest()
                    self.entered.set()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(asyncio.wrap_future(self.release_signal)), timeout=10,
                        )
                        self.released_by_test = True
                    except TimeoutError:
                        self.timed_out = True
                    # Forward the actual original message. A send failure remains a fixture failure.
                    await send(message)
                    self.sent.set()
                    return
            await send(message)

        await self.app(scope, receive, held_send)


def _route_feedback_snapshot(store, job_id):
    """Retain only synthetic identity/state columns needed by the no-submission oracle."""
    with store.engine.connect() as connection:
        routes = [dict(row) for row in connection.execute(
            select(m.routes.c.id, m.routes.c.url, m.routes.c.provider, m.routes.c.tenant)
            .where(m.routes.c.job_id == job_id).order_by(m.routes.c.id)
        ).mappings()]
        counts = {name: connection.execute(select(func.count()).select_from(table)).scalar_one()
                  for name, table in (("applications", m.applications),
                                      ("attempts", m.attempts), ("operations", m.operations))}
        saved_events = connection.execute(select(func.count()).select_from(m.audit).where(
            m.audit.c.entity_id == job_id,
            m.audit.c.action == "APPLICATION_ROUTE_DESTINATION_SAVED",
        )).scalar_one()
    return {"routes": routes, "counts": counts, "saved_events": saved_events}


@pytest.mark.skipif(not (ROOT / "apps/web/dist/index.html").is_file(),
                    reason="Build apps/web before browser acceptance")
def test_pending_route_save_blocks_recheck_and_later_result_starts_unsaved(record_property, request):
    """A delayed real save retains A; a later explicit B check is visibly unsaved."""
    job_id = "d" * 64
    route_id = "route-feedback-board"
    company_id = "route-feedback-company"
    candidate_a = "https://boards.greenhouse.io/routefeedback/jobs/1"
    candidate_b = "https://boards.greenhouse.io/routefeedback/jobs/2"
    source_url = "https://board.example/jobs/route-feedback"
    resolve_path = f"/jobs/{job_id}/routes/{route_id}/resolve"
    save_path = f"/jobs/{job_id}/routes/{route_id}/save-verified-destination"
    visit_path = f"/profiles/{PROFILE['id']}/jobs/{job_id}/view"
    allowed_posts = {visit_path, resolve_path, save_path}
    traffic = {"post_paths": [], "unexpected_mutations": [], "external_requests": 0, "page_errors": 0}
    evidence = {"schema_version": 1, "scope": "pending-save-check-interlock",
                "job_id": job_id, "source_route_id": route_id, "phases": [],
                "resolver": "controlled synthetic return; no provider request",
                "behavior_complete": False}
    cleanup = {"schema_version": 1, "gate_released": False, "context_closed": False,
               "browser_disconnected": False, "playwright_stopped": False,
               "server_thread_stopped": False, "socket_closed": False,
               "store_disposed": False, "resolver_stopped": False,
               "temporary_directory_removed": False, "error_types": []}
    directory = store = route_resolution = gate = sock = server = thread = None
    playwright = browser = context = None
    primary_failure = False
    try:
        directory = tempfile.TemporaryDirectory(prefix="mjobs-route-feedback-")
        store = Store(f"sqlite:///{(Path(directory.name) / 'browser.db').as_posix()}")
        store.migrate()
        store.bind_workspace_mode("PERSONAL")
        profile = copy.deepcopy(PROFILE)
        profile["preferences"] = {**profile.get("preferences", {}),
                                  "application_route_preference": "USER_CHOICE"}
        store.save_profile(profile)
        source_id = configure_source(store, "public", provider="remotive")
        with store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company_id, "name": "Route Feedback Employer", "domain": "routefeedback.example",
            })
            upsert(connection, m.jobs, {
                "id": job_id, "company_id": company_id, "requisition": "route-feedback",
                "title": "Route Save Feedback Engineer", "description": "Support local systems.",
                "skills": ["Python"], "location": "Remote", "remote_type": "REMOTE",
                "attributes": {"source_id": source_id, "provider": "remotive", "tenant": "public"},
            })
            upsert(connection, m.routes, {
                "id": route_id, "job_id": job_id, "company_id": company_id,
                "provider": "job-board", "tenant": "integrated-board", "requisition": "route-feedback",
                "url": source_url,
            })
            upsert(connection, m.company_ats_tenants, {
                "id": "route-feedback-tenant", "company_id": company_id,
                "provider": "greenhouse", "tenant_identifier": "routefeedback",
                "organization_identifier": "", "board_identifier": "", "confidence": 0.94,
                "evidence": ["PUBLIC_API_BOARD"], "first_seen_at": "2026-10-09T00:00:00Z",
                "last_seen_at": "2026-10-09T00:00:00Z",
            })
            connection.execute(insert(m.postings).values(
                id="route-feedback-posting", job_id=job_id, source="job-board",
                source_job_id="route-feedback", source_url=source_url, application_url=source_url,
                raw={}, fetched_at="2026-10-09T00:00:00Z",
            ))
        board_source = upsert_company_job_source(
            store, company_id=company_id, provider="remotive", tenant_identifier="public",
            public_api_url="https://remotive.com/api/remote-jobs", collection_strategy="PUBLIC_JSON",
        )
        with store.engine.begin() as connection:
            connection.execute(m.company_sources.update().where(m.company_sources.c.id == board_source["id"])
                               .values(collector_source_id=source_id))
            connection.execute(insert(m.postings).values(
                id="route-feedback-board-posting", job_id=job_id,
                source="https://remotive.com/api/remote-jobs", source_job_id="route-feedback-board-copy",
                source_url="https://remotive.com/api/remote-jobs",
                application_url="https://remotive.com/remote-jobs/example/1",
                raw={}, fetched_at="2026-10-09T00:00:00Z",
            ))
        app = create_app(store, "http://127.0.0.1:1", ROOT,
                         "redis://127.0.0.1:1/0", operation_mode="PERSONAL")
        route_resolution = patch("apps.api.main.resolve_aggregator_redirect", return_value={
            "status": "RESOLVED_ATS_ROUTE", "candidate_url": candidate_a,
            "provider": "greenhouse", "tenant": "routefeedback",
        })
        resolver = route_resolution.start()
        gate = _RouteSaveResponseGate(app, save_path)
        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(gate, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "Route feedback API did not start"
            time.sleep(0.02)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()

        def local_only(route):
            parsed = urlsplit(route.request.url)
            method = route.request.method
            if parsed.scheme != "http" or parsed.netloc != urlsplit(origin).netloc:
                traffic["external_requests"] += 1
                route.abort()
                return
            if method == "POST":
                traffic["post_paths"].append(parsed.path)
            if method not in {"GET", "HEAD", "OPTIONS"} and not (
                    method == "POST" and parsed.path in allowed_posts):
                traffic["unexpected_mutations"].append({"method": method, "path": parsed.path})
                route.abort()
                return
            route.continue_()

        context.route("**/*", local_only)
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda _error: traffic.__setitem__("page_errors", traffic["page_errors"] + 1))
        page.goto(origin, wait_until="domcontentloaded")
        page.get_by_label("Active profile", exact=True).select_option(str(PROFILE["id"]))
        page.get_by_role("button", name="Profile", exact=True).click()
        expect(page.get_by_role("radio", name="Ask me to choose a route for each job")).to_be_checked()
        page.get_by_role("button", name=re.compile(r"^Jobs(?:\s|$)")).click()
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and urlsplit(response.url).path == visit_path) as visit:
            page.get_by_role("button", name=re.compile("Route Save Feedback Engineer")).click()
        assert visit.value.ok and visit.value.finished() is None
        route_picker = page.get_by_label("Choose the application route for this job")
        route_picker.select_option(route_id)
        before = _route_feedback_snapshot(store, job_id)
        assert before["counts"] == {"applications": 0, "attempts": 0, "operations": 0}
        assert len(before["routes"]) == 1 and before["routes"][0]["id"] == route_id
        assert before["saved_events"] == 0
        check_route = page.get_by_role("button", name="Check employer route", exact=True)
        link = page.get_by_role("link", name="Review detected ATS destination ↗")
        saved_notice = page.get_by_text(
            "Saved as a selectable route. Your current route selection is unchanged. "
            "Review the option above; preparation still follows your saved route preference. Nothing is submitted.",
            exact=True,
        )
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and urlsplit(response.url).path == resolve_path) as checked_a:
            check_route.click()
        assert checked_a.value.ok
        expect(link).to_have_attribute("href", candidate_a)
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and urlsplit(response.url).path == save_path) as saved:
            page.get_by_role("button", name="Save detected destination as selectable route", exact=True).click()
        assert gate.entered.wait(timeout=5), "Real save response never reached the post-commit gate"
        held = _route_feedback_snapshot(store, job_id)
        assert gate.status == 201 and gate.receipt["created"] is True
        assert gate.receipt["status"] == "RESOLVED_ATS_ROUTE"
        assert gate.receipt["source_route_id"] == route_id and gate.receipt["route_url"] == candidate_a
        saved_id = gate.receipt["route_id"]
        assert {row["id"]: row["url"] for row in held["routes"]} == {
            route_id: source_url, saved_id: candidate_a,
        }
        assert held["counts"] == before["counts"] and held["saved_events"] == 1
        assert next(row for row in held["routes"] if row["id"] == route_id) == before["routes"][0]
        assert not gate.sent.is_set() and not gate.release_requested and not gate.timed_out
        resolver_calls_at_gate = resolver.call_count
        assert resolver_calls_at_gate == 2  # Initial check A, then the production save verification.
        expect(check_route).to_be_disabled()
        expect(page.get_by_role("button", name="Saving verified destination…", exact=True)).to_be_disabled()
        expect(saved_notice).to_have_count(0)
        box = check_route.bounding_box()
        assert box is not None
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        expect(link).to_have_attribute("href", candidate_a)
        expect(route_picker).to_have_value(route_id)
        assert traffic["post_paths"].count(resolve_path) == 1
        assert resolver.call_count == resolver_calls_at_gate
        evidence["phases"].append({
            "phase": "pending_save", "durable_saved_route_id": saved_id, "durable_url": candidate_a,
            "displayed_url": link.get_attribute("href"), "selected_route_id": route_picker.input_value(),
            "check_disabled": check_route.is_disabled(), "ordinary_click_attempted": True,
            "resolve_posts": traffic["post_paths"].count(resolve_path),
            "save_posts": traffic["post_paths"].count(save_path),
            "resolver_calls_at_gate": resolver_calls_at_gate,
            "resolver_calls_after_click": resolver.call_count,
            "body_withheld": not gate.sent.is_set(), "database": held,
        })
        gate.release()
        assert saved.value.status == 201
        saved_body = saved.value.json()
        assert saved_body["route"]["id"] == saved_id and saved_body["route"]["url"] == candidate_a
        assert gate.sent.wait(timeout=5) and gate.released_by_test and not gate.timed_out
        expect(saved_notice).to_be_visible()
        expect(check_route).to_be_enabled()
        expect(link).to_have_attribute("href", candidate_a)
        expect(route_picker).to_have_value(route_id)
        expect(route_picker.locator(f"option[value='{saved_id}']")).to_be_attached()
        after_save = _route_feedback_snapshot(store, job_id)
        assert after_save == held
        evidence["phases"].append({
            "phase": "saved_a", "displayed_url": link.get_attribute("href"),
            "selected_route_id": route_picker.input_value(), "saved_notice": saved_notice.is_visible(),
            "response_route_id": saved_body["route"]["id"], "response_url": saved_body["route"]["url"],
            "saved_option_present": route_picker.locator(f"option[value='{saved_id}']").count() == 1,
            "database": after_save,
        })
        resolver.return_value = {**resolver.return_value, "candidate_url": candidate_b}
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and urlsplit(response.url).path == resolve_path) as checked_b:
            check_route.click()
        assert checked_b.value.ok
        expect(link).to_have_attribute("href", candidate_b)
        expect(saved_notice).to_have_count(0)
        expect(page.get_by_role("button", name="Save detected destination as selectable route",
                                exact=True)).to_be_enabled()
        expect(route_picker).to_have_value(route_id)
        after_check = _route_feedback_snapshot(store, job_id)
        assert after_check == held  # Merely checking B cannot save B or change application state.
        assert traffic["post_paths"].count(visit_path) == 1
        assert traffic["post_paths"].count(resolve_path) == 2
        assert traffic["post_paths"].count(save_path) == 1
        assert len(traffic["post_paths"]) == 4 and resolver.call_count == 3
        assert traffic["unexpected_mutations"] == [] and traffic["external_requests"] == 0
        assert traffic["page_errors"] == 0 and gate.target_requests == 1
        evidence["phases"].append({
            "phase": "checked_b_unsaved", "displayed_url": link.get_attribute("href"),
            "selected_route_id": route_picker.input_value(), "saved_notice_count": saved_notice.count(),
            "save_enabled": page.get_by_role("button",
                name="Save detected destination as selectable route", exact=True).is_enabled(),
            "database": after_check,
        })
        evidence["resolver_calls_final"] = resolver.call_count
        evidence["before"] = before
        evidence["behavior_complete"] = True
    except BaseException:
        primary_failure = True
        raise
    finally:
        # Copy the actual observation before any fixture cleanup can change it.
        evidence["traffic"] = copy.deepcopy(traffic)
        evidence["gate"] = None if gate is None else {
            "target_requests": gate.target_requests, "status": gate.status,
            "receipt": gate.receipt, "body_sha256": gate.body_sha256,
            "entered": gate.entered.is_set(), "sent": gate.sent.is_set(),
            "released_by_test": gate.released_by_test, "timed_out": gate.timed_out,
        }
        request.node.route_save_feedback_receipt = copy.deepcopy(evidence)
        record_property("route_save_feedback_receipt", json.dumps(evidence, sort_keys=True))

        def clean(label, action):
            try:
                action()
                cleanup[label] = True
            except BaseException as error:
                cleanup["error_types"].append({"step": label, "type": type(error).__name__})

        if gate is not None:
            clean("gate_released", gate.release)
        if context is not None:
            clean("context_closed", context.close)
        if browser is not None:
            def close_browser():
                browser.close()
                assert not browser.is_connected()
            clean("browser_disconnected", close_browser)
        if playwright is not None:
            clean("playwright_stopped", playwright.stop)
        if server is not None:
            server.should_exit = True
        if thread is not None:
            def join_server():
                thread.join(timeout=5)
                assert not thread.is_alive()
            clean("server_thread_stopped", join_server)
        if sock is not None:
            def close_socket():
                sock.close()
                assert sock.fileno() == -1
            clean("socket_closed", close_socket)
        if store is not None:
            clean("store_disposed", store.engine.dispose)
        if route_resolution is not None:
            clean("resolver_stopped", route_resolution.stop)
        if directory is not None:
            def remove_temporary_directory():
                directory_path = Path(directory.name)
                directory.cleanup()
                assert not directory_path.exists()
            clean("temporary_directory_removed", remove_temporary_directory)
        cleanup["primary_failure_preserved"] = primary_failure
        cleanup["all_resources_closed"] = all(cleanup[key] for key in (
            "gate_released", "context_closed", "browser_disconnected", "playwright_stopped",
            "server_thread_stopped", "socket_closed", "store_disposed", "resolver_stopped",
            "temporary_directory_removed")) and not cleanup["error_types"]
        request.node.route_save_feedback_cleanup = copy.deepcopy(cleanup)
        record_property("route_save_feedback_cleanup", json.dumps(cleanup, sort_keys=True))
        if not primary_failure:
            assert cleanup["all_resources_closed"], "Route feedback fixture cleanup did not complete"
