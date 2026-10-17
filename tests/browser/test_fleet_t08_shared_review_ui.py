"""Actual main UI -> API -> worker -> durable, value-free Breezy review."""
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
import uvicorn

from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from services.applications.adapters.breezy import BreezyApplicationAdapter
from tests.integration import test_fleet_t06_breezy_connected as breezy_flow


ROOT = Path(__file__).resolve().parents[2]
PRIVATE_VALUE = "PRIVATE_T08_BROWSER_FORM_VALUE"
FORM_HTML = (
    "<form><label for='email'>Email address</label>"
    f"<input id='email' name='email' type='email' required value='{PRIVATE_VALUE}'>"
    "</form>"
).encode()


def test_main_review_click_reaches_worker_and_survives_reload_without_values():
    assert (ROOT / "apps/web/dist/index.html").is_file(), "Build the frozen UI before acceptance"
    flow = breezy_flow.FleetT06BreezyConnectedTests(methodName="setUp")
    flow.setUp()
    server = None
    thread = None
    sock = None
    try:
        app = flow.flow.client.app
        review_path = flow._path()
        capability_path = review_path.removesuffix("/form-reviews") + "/form-review-capabilities"
        executions = []
        api_requests = []

        @app.middleware("http")
        async def execute_synthetic_queue(request, call_next):
            api_requests.append((request.method, request.url.path))
            response = await call_next(request)
            if request.method == "POST" and request.url.path == review_path and response.status_code == 202:
                with flow.store.engine.connect() as connection:
                    operation = dict(connection.execute(select(m.operations).where(
                        m.operations.c.kind == "APPLICATION_FORM_REVIEW",
                        m.operations.c.entity_id == flow.application_id,
                    )).mappings().one())
                # Only the bounded public fetch is synthetic. Queue admission,
                # claimed worker, adapter, receipt and readback are production paths.
                result, fetched, constructed = flow._execute(operation["id"], html=FORM_HTML)
                executions.append((operation, result, fetched, constructed))
            return response

        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "Synthetic API did not start"
            time.sleep(0.02)

        application = flow.store.application(flow.application_id)
        job = next(row for row in flow.store.jobs("fixture") if row["id"] == application["job_id"])
        outside = []
        page_errors = []
        analyze = BreezyApplicationAdapter.analyze_form
        with patch.object(BreezyApplicationAdapter, "analyze_form", autospec=True,
                          side_effect=analyze) as selected, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

                def local_only(route):
                    if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                        outside.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", local_only)
                page = context.new_page()
                page.set_default_timeout(8000)
                page.on("pageerror", lambda error: page_errors.append(str(error)))

                def open_application():
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    pipeline = page.get_by_role("region", name="Application pipeline", exact=True)
                    pipeline.get_by_role("button", name=job["title"], exact=False).click()
                    expect(page.get_by_role("heading", name="Application details", exact=True)).to_be_visible()

                open_application()
                review = page.get_by_role("region", name="Application form review", exact=True)
                inspect = review.get_by_role("button", name="Inspect form fields (read-only)", exact=True)
                expect(inspect).to_be_enabled()
                assert executions == []
                assert flow.store.count("worker_operations") == 0
                inspect.click()
                expect(review.get_by_role("listitem")).to_contain_text("email")
                assert len(executions) == 1
                operation, result, fetched, constructed = executions[0]
                assert result["status"] == "COMPLETED"
                assert fetched == constructed == [flow.normalized_route["url"]]
                selected.assert_called_once()
                assert selected.call_args.kwargs["route_url"] == flow.normalized_route["url"]
                request_id = operation["params"]["request_id"]

                exact_response = page.request.get(origin + review_path + "/requests/" + request_id)
                latest_response = page.request.get(origin + review_path + "/latest")
                assert exact_response.status == latest_response.status == 200
                exact = exact_response.json()
                assert exact == latest_response.json()
                assert exact["state"] == "COMPLETED"
                assert exact["analysis"]["status"] == "REVIEW_REQUIRED"
                audits = flow._review_audits()
                assert len(audits) == 1
                assert audits[0]["payload"]["analysis"] == exact["analysis"]
                assert flow._attempt_count() == 0
                assert PRIVATE_VALUE not in json.dumps((exact, audits))
                assert PRIVATE_VALUE not in page.locator("body").inner_text()

                # Reopen the physical SQLite store, then hydrate a fresh page
                # through the real API after disposing its old connection pool.
                flow.store.engine.dispose()
                reopened = flow.flow._new_store()
                try:
                    assert flow._review_audits(reopened) == audits
                    assert flow._operation(operation["id"], reopened)["state"] == "COMPLETED"
                    assert flow._attempt_count(reopened) == 0
                finally:
                    reopened.engine.dispose()
                open_application()
                review = page.get_by_role("region", name="Application form review", exact=True)
                expect(review.get_by_role("listitem")).to_contain_text("email")
                assert len(executions) == selected.call_count == 1
                assert flow._review_audits() == audits
                assert PRIVATE_VALUE not in page.locator("body").inner_text()

                # A current raw route rejection disables the affordance without
                # fetching or discarding the historical durable receipt.
                flow._set_route(url=" " + flow.normalized_route["url"])
                before = (flow.store.count("worker_operations"), flow._audit_count(), flow._attempt_count())
                capability = page.request.get(origin + capability_path)
                assert capability.status == 200
                assert capability.json()["review_supported"] is False
                open_application()
                unavailable = page.get_by_role("region", name="Application form review", exact=True)
                expect(unavailable).to_contain_text("Form inspection is unavailable for this application.")
                expect(unavailable.get_by_role(
                    "button", name="Inspect form fields (read-only)", exact=True,
                )).to_be_disabled()
                expect(unavailable.locator('[aria-label="Inspected form fields"]')).to_have_count(0)
                assert (flow.store.count("worker_operations"), flow._audit_count(), flow._attempt_count()) == before
                historical = page.request.get(origin + review_path + "/requests/" + request_id)
                assert historical.status == 200 and historical.json() == exact
                assert len(executions) == selected.call_count == 1
                assert ("GET", capability_path) in api_requests
                assert sum(method == "POST" and path == review_path for method, path in api_requests) == 1
                assert outside == []
                assert page_errors == []
                context.close()
            finally:
                browser.close()
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.ident is not None:
            thread.join(timeout=10)
        if sock is not None:
            sock.close()
        try:
            assert thread is None or not thread.is_alive(), "Synthetic API must stop"
        finally:
            flow.doCleanups()
