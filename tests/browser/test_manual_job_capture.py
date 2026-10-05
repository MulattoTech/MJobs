"""Synthetic browser/API proof for manual capture and preparation handoff."""
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class PrivateUrlProbe(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.requests.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


class ManualJobCaptureBrowserTests(unittest.TestCase):
    def test_manual_capture_persists_distinct_jobs_and_prepares_without_fetching(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-manual-capture-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'trial.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            PrivateUrlProbe.requests = []
            probe = HTTPServer(("127.0.0.1", 0), PrivateUrlProbe)
            probe_thread = threading.Thread(target=probe.serve_forever, daemon=True)
            probe_thread.start()
            private_reference_url = f"http://127.0.0.1:{probe.server_port}/private/job/42"
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated manual-capture API did not start")
                    time.sleep(0.02)

                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    external = []
                    fail_next_rescore = [False]

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    def execute_rescore(route):
                        response = route.fetch()
                        operation_id = response.json()["operation_id"]
                        if fail_next_rescore[0]:
                            fail_next_rescore[0] = False
                            route.abort("failed")
                            return
                        with patch.dict("os.environ", MJOBS_DATABASE_URL=store.url,
                                        MJOBS_OPERATION_MODE="FIXTURE",
                                        MJOBS_OUTPUT_DIR=str(root)):
                            from workers.tasks import execute_operation
                            result = execute_operation(operation_id)
                        self.assertEqual(result["status"], "COMPLETED", result)
                        route.fulfill(response=response)

                    context.route("**/matches", execute_rescore)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_text("External submission OFF", exact=True)).to_be_visible()

                    page.get_by_role("button", name="Capture a job", exact=True).first.click()
                    page.get_by_label("Job title", exact=True).fill("Systems Administrator")
                    page.get_by_label("Company", exact=True).fill("Example Infrastructure")
                    page.get_by_label("Job description", exact=True).fill(
                        "Maintain Windows Server and Active Directory.\nUnknown field: preserve verbatim.")
                    page.get_by_label("Location", exact=True).fill("Tulsa, OK")
                    page.get_by_label("Job reference URL", exact=True).fill(
                        private_reference_url)
                    page.get_by_label("Application URL", exact=True).fill(
                        "https://apply.example.invalid/role/42")
                    page.get_by_label("Other source fields (JSON)", exact=False).fill(
                        '{"original_level":"unspecified","custom":{"keep":true}}')
                    with page.expect_response(
                        lambda response: response.url == origin + "/jobs/manual"
                        and response.request.method == "POST"
                    ) as captured:
                        page.get_by_role("button", name="Save captured job", exact=True).click()
                    first_response = captured.value
                    self.assertEqual(first_response.status, 201)
                    first = first_response.json()
                    first_id = first["job"]["id"]
                    request_payload = first_response.request.post_data_json
                    expect(page.get_by_role("status").filter(has_text="Job captured and evaluated")).to_be_visible()

                    card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(card).to_be_visible()
                    card.click()
                    detail = page.locator(".job-detail")
                    expect(detail).to_contain_text("Maintain Windows Server and Active Directory.")
                    expect(detail).to_contain_text("manual")
                    expect(detail).to_contain_text("Weighted coverage of detected skill evidence")
                    expect(detail.get_by_role("list", name="Skill match explanation")).to_contain_text("UNKNOWN")
                    expect(detail.get_by_role("list", name="Skill match explanation")).to_contain_text("Active Directory")
                    expect(detail.get_by_role("list", name="Skill match explanation")).to_contain_text("Windows Server")
                    self.assertEqual(first["job"]["skills"], ["Windows Server", "Active Directory"])
                    expect(detail.get_by_role("link", name="Open employer application", exact=False)).to_have_attribute(
                        "href", "https://apply.example.invalid/role/42")
                    self.assertEqual(store.job(first_id)["description"],
                                     "Maintain Windows Server and Active Directory.\nUnknown field: preserve verbatim.")
                    persisted_match = store.job(first_id, profile_id="fixture")["match"]
                    self.assertEqual([row["strength"] for row in persisted_match["components"]["requirements"]],
                                     ["UNKNOWN", "UNKNOWN"])
                    raw = store.job(first_id)["postings"][0]["raw"]
                    self.assertEqual(raw["submitted_fields"]["source_details"],
                                     {"original_level": "unspecified", "custom": {"keep": True}})
                    self.assertEqual(raw["submitted_fields"]["reference_url"], private_reference_url)

                    retry = context.request.post(origin + "/jobs/manual", data=request_payload)
                    self.assertEqual(retry.status, 200)
                    self.assertEqual(retry.json()["job"]["id"], first_id)
                    conflict = context.request.post(origin + "/jobs/manual", data={
                        **request_payload, "title": "Different title under the same key",
                    })
                    self.assertEqual(conflict.status, 409)
                    duplicate_payload = {**request_payload, "request_id": "deliberate-second-capture"}
                    duplicate = context.request.post(origin + "/jobs/manual", data=duplicate_payload)
                    self.assertEqual(duplicate.status, 201)
                    self.assertNotEqual(duplicate.json()["job"]["id"], first_id)
                    self.assertEqual(len(store.jobs("fixture", sort="title")), 2)
                    missing_link = context.request.post(origin + "/jobs/manual", data={
                        **request_payload, "request_id": "missing-link", "reference_url": None,
                        "application_url": None,
                    })
                    self.assertEqual(missing_link.status, 422)
                    unsafe_scheme = context.request.post(origin + "/jobs/manual", data={
                        **request_payload, "request_id": "unsafe-scheme",
                        "reference_url": "javascript:alert(1)",
                    })
                    self.assertEqual(unsafe_scheme.status, 422)
                    padded_request_id = context.request.post(origin + "/jobs/manual", data={
                        **request_payload, "request_id": " padded-key ",
                    })
                    self.assertEqual(padded_request_id.status, 422)
                    reference_only_payload = {
                        **request_payload,
                        "request_id": "reference-without-application-route",
                        "title": "Reference only operator role",
                        "company": "Reference only employer",
                        "reference_url": "https://jobs.example.invalid/role/43",
                        "application_url": None,
                    }
                    reference_only = context.request.post(
                        origin + "/jobs/manual", data=reference_only_payload)
                    self.assertEqual(reference_only.status, 201)
                    application_only_payload = {
                        **request_payload,
                        "request_id": "application-link-without-reference",
                        "title": "Application link only operator role",
                        "company": "Application link only employer",
                        "reference_url": None,
                        "application_url": "https://apply.example.invalid/role/44",
                    }
                    application_only = context.request.post(
                        origin + "/jobs/manual", data=application_only_payload)
                    self.assertEqual(application_only.status, 201)

                    page.reload(wait_until="domcontentloaded")
                    card = page.locator(".job-card").filter(has_text="Systems Administrator").first
                    expect(card).to_be_visible()
                    card.click()
                    expect(page.locator(".job-detail").get_by_role("list", name="Skill match explanation")).to_contain_text("no approved profile evidence")
                    card = page.locator(".job-card").filter(
                        has_text="Reference only operator role").first
                    expect(card).to_be_visible()
                    card.click()
                    detail = page.locator(".job-detail")
                    expect(detail.get_by_role("link", name="Open saved reference", exact=False)).to_have_attribute(
                        "href", "https://jobs.example.invalid/role/43")
                    expect(detail.get_by_role("link", name="Open employer application", exact=False)).to_have_count(0)
                    expect(detail.get_by_role("link", name="Open saved application link", exact=False)).to_have_count(0)
                    expect(page.get_by_role("button", name="Prepare documents", exact=True)).to_be_disabled()
                    expect(detail).to_contain_text("A job reference alone is not an application route")
                    rejected_prepare = context.request.post(origin + "/applications", data={
                        "profile_id": "fixture", "request_id": "reference-only-preparation-rejected",
                        "job_id": reference_only.json()["job"]["id"],
                    })
                    self.assertEqual(rejected_prepare.status, 422)
                    card = page.locator(".job-card").filter(
                        has_text="Application link only operator role").first
                    card.click()
                    detail = page.locator(".job-detail")
                    expect(detail).to_contain_text("Application link only operator role")
                    expect(detail.get_by_role("link", name="Open saved reference", exact=False)).to_have_count(0)
                    expect(detail.get_by_role("link", name="Open employer application", exact=False)).to_have_attribute(
                        "href", "https://apply.example.invalid/role/44")
                    card = page.locator(".job-card").filter(has_text="Systems Administrator").first
                    card.click()
                    with page.expect_response(
                        lambda response: response.url == origin + "/applications"
                        and response.request.method == "POST"
                    ) as queued:
                        page.get_by_role("button", name="Prepare documents", exact=True).click()
                    operation = queued.value.json()
                    with patch.dict("os.environ", MJOBS_DATABASE_URL=store.url,
                                    MJOBS_MOCK_ORIGIN="http://127.0.0.1:1",
                                    MJOBS_OUTPUT_DIR=str(root), MJOBS_OPERATION_MODE="FIXTURE"):
                        from workers.tasks import execute_operation
                        self.assertEqual(execute_operation(operation["operation_id"])["status"], "COMPLETED")
                    expect(page.get_by_role("status").filter(has_text="Preparing factual documents")).not_to_be_visible()
                    self.assertEqual(len(store.applications("fixture")), 1)
                    application = store.applications("fixture")[0]
                    self.assertEqual(application["target"], "REAL")
                    self.assertEqual(application["state"], "PREPARED")
                    self.assertIsNone(application["confirmation"])

                    fail_next_rescore[0] = True
                    page.get_by_role("navigation", name="Main navigation").get_by_role("button").first.click()
                    page.get_by_role("button", name="Capture a job", exact=True).click()
                    page.get_by_label("Job title", exact=True).fill("Second systems role")
                    page.get_by_label("Company", exact=True).fill("Other Infrastructure")
                    page.get_by_label("Job description", exact=True).fill("Maintain Active Directory.")
                    page.get_by_label("Job reference URL", exact=True).fill("https://jobs.example.invalid/role/99")
                    page.get_by_label("Application URL", exact=True).fill("https://apply.example.invalid/role/99")
                    with page.expect_response(
                        lambda response: response.url == origin + "/jobs/manual"
                        and response.request.method == "POST"
                    ) as failed_rescore_capture:
                        page.get_by_role("button", name="Save captured job", exact=True).click()
                    saved_capture = failed_rescore_capture.value.json()
                    self.assertEqual(failed_rescore_capture.value.status, 201)
                    expect(page.locator(".notice").filter(
                        has_text="Job saved; match refresh failed.")).to_contain_text(
                        "Job saved; match refresh failed. The capture remains in your Jobs list.")
                    expect(page.locator(".job-card").filter(has_text="Second systems role")).to_be_visible()
                    self.assertEqual(store.job(saved_capture["job"]["id"])["title"], "Second systems role")
                    self.assertEqual(external, [])
                    self.assertEqual(PrivateUrlProbe.requests, [])
                    self.assertEqual(store.count("application_attempts"), 0)
                    page.close()
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                probe.shutdown()
                probe.server_close()
                probe_thread.join(timeout=10)
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
                self.assertFalse(probe_thread.is_alive())
