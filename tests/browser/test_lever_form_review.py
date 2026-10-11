"""Connected Chromium acceptance for durable Lever form review in application details."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


ROOT = Path(__file__).resolve().parents[2]
LEVER_ROUTE = "https://jobs.lever.co/example/12345678-abcd-1234-abcd-1234567890ab"
FORM_FIXTURE = (ROOT / "tests" / "fixtures" / "lever_application_form.html").read_bytes()
PRIVATE_MARKERS = (
    b"PRIVATE-DECOY-APPLICANT",
    b"private-applicant@example.invalid",
    b"PRIVATE-DECOY-GENDER",
)


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class LeverFormReviewBrowserTests(unittest.TestCase):
    def test_persisted_lever_route_inspects_safely_and_reads_back_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-lever-review-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'lever-review.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
            job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
            application_id = prepare(store, job["id"], "fixture", root)
            application = store.application(application_id)
            with store.engine.begin() as connection:
                connection.execute(update(m.routes).where(
                    m.routes.c.id == application["route_id"],
                ).values(
                    provider="lever", tenant="example",
                    requisition="12345678-abcd-1234-abcd-1234567890ab",
                    url=LEVER_ROUTE,
                ))
                connection.execute(update(m.applications).where(
                    m.applications.c.id == application_id,
                ).values(target="REAL"))

            before_application = store.application(application_id)
            before_documents = store.count("documents")
            before_attempts = store.count("application_attempts")
            app = create_app(store, "http://127.0.0.1:1", root,
                             operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(
                app, log_level="error", access_log=False))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            external_requests, browser_writes, request_bodies, errors = [], [], [], []
            fetched_urls = []
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated Lever form-review API did not start")
                    time.sleep(0.02)

                class FixtureResponse:
                    url = LEVER_ROUTE
                    headers = {"content-type": "text/html; charset=utf-8"}
                    content = FORM_FIXTURE

                class FixturePublicClient:
                    def __init__(self, website, check=None, total_timeout=None):
                        self.website = website
                        self.check = check

                    def allowed(self, url):
                        return url == LEVER_ROUTE

                    def get(self, url):
                        if url != LEVER_ROUTE:
                            raise AssertionError("Review worker requested an unexpected route")
                        fetched_urls.append(url)
                        if self.check:
                            self.check()
                        return FixtureResponse()

                    def close(self):
                        pass

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1365, "height": 950})
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def record_request(request):
                        if request.method not in {"GET", "HEAD", "OPTIONS"}:
                            browser_writes.append((request.method, urlsplit(request.url).path))
                            if request.post_data:
                                try:
                                    request_bodies.append(json.loads(request.post_data))
                                except (TypeError, json.JSONDecodeError):
                                    pass

                    page.on("request", record_request)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    pipeline = page.get_by_role("region", name="Application pipeline")
                    pipeline.get_by_role("button", name=job["title"], exact=False).click()

                    application_review = page.get_by_role(
                        "region", name="Application review", exact=True)
                    expect(application_review).to_contain_text("lever · example")
                    review = page.get_by_role("region", name="Application form review")
                    inspect = review.get_by_role(
                        "button", name="Inspect form fields (read-only)", exact=True)
                    expect(inspect).to_be_visible()
                    expect(review).to_contain_text(
                        "Field values are never displayed or changed.")
                    self.assertEqual(browser_writes, [],
                                     "Opening details must not start an application operation")
                    self.assertEqual(store.application(application_id), before_application)

                    review_path = (
                        f"/profiles/fixture/applications/{application_id}/form-reviews")
                    with page.expect_response(lambda response:
                            response.request.method == "POST"
                            and urlsplit(response.url).path == review_path) as queued_response:
                        inspect.click()
                    queued = queued_response.value
                    self.assertEqual(queued.status, 202, queued.text())
                    request_id = queued.json()["request_id"]
                    operation_id = queued.json()["operation_id"]
                    self.assertEqual(queued.request.post_data_json, {"request_id": request_id})

                    with patch.dict(os.environ, {
                        "MJOBS_OPERATION_MODE": "FIXTURE",
                        "MJOBS_DATABASE_URL": f"sqlite:///{(root / 'lever-review.db').as_posix()}",
                        "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
                        "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
                        "MJOBS_OUTPUT_DIR": str(root / "documents"),
                    }), patch("workers.tasks.PublicCompanyClient", FixturePublicClient):
                        result = execute_operation(operation_id)
                    self.assertEqual(result["status"], "COMPLETED", result)
                    self.assertEqual(fetched_urls, [LEVER_ROUTE])

                    fields_region = review.get_by_role(
                        "region", name="Inspected form fields")
                    expect(fields_region).to_contain_text("first_name")
                    expect(fields_region).to_contain_text("gender")
                    gender_field = fields_region.locator("li").filter(has_text="gender")
                    expect(gender_field).to_contain_text("Review required")
                    for marker in (marker.decode("utf-8") for marker in PRIVATE_MARKERS):
                        self.assertNotIn(marker, page.content())

                    durable = page.request.get(
                        f"{origin}{review_path}/{request_id}")
                    self.assertEqual(durable.status, 200, durable.text())
                    durable_payload = durable.json()
                    self.assertEqual(durable_payload["state"], "COMPLETED")
                    self.assertEqual(durable_payload["analysis"]["status"], "REVIEW_REQUIRED")
                    self.assertTrue(any(
                        field["name"] == "gender"
                        and field["review_reason"] == "SENSITIVE_FIELD"
                        for field in durable_payload["analysis"]["fields"]))
                    self.assertFalse(any(marker in durable.content for marker in PRIVATE_MARKERS))

                    # A full app reload and reopening the persisted Lever application
                    # must recover the completed safe inspection from durable state.
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Applications", exact=False).click()
                    pipeline = page.get_by_role("region", name="Application pipeline")
                    pipeline.get_by_role("button", name=job["title"], exact=False).click()
                    application_review = page.get_by_role(
                        "region", name="Application review", exact=True)
                    expect(application_review).to_contain_text("lever · example")
                    review = page.get_by_role("region", name="Application form review")
                    fields_region = review.get_by_role(
                        "region", name="Inspected form fields")
                    expect(fields_region).to_contain_text("first_name")
                    gender_field = fields_region.locator("li").filter(has_text="gender")
                    expect(gender_field).to_contain_text("Review required")
                    for marker in (marker.decode("utf-8") for marker in PRIVATE_MARKERS):
                        self.assertNotIn(marker, page.content())

                    self.assertEqual(browser_writes, [("POST", review_path)], browser_writes)
                    self.assertEqual(len(request_bodies), 1)
                    self.assertEqual(set(request_bodies[0]), {"request_id"})
                    self.assertEqual(external_requests, [])
                    self.assertEqual(errors, [])
                    self.assertFalse(page.get_by_role(
                        "button", name="Submit application", exact=False).count())
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), "Isolated Lever review server must stop")
            self.assertEqual(store.application(application_id), before_application)
            self.assertEqual(store.count("documents"), before_documents)
            self.assertEqual(store.count("application_attempts"), before_attempts)


if __name__ == "__main__":
    unittest.main()
