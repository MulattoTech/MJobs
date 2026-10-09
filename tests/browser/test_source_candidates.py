"""Connected synthetic browser proof for source capture, review, reload, and isolation."""
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
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs
from tests.integration.test_spine import PROFILE


class SourceUrlProbe(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.requests.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


class SourceCandidateBrowserTests(unittest.TestCase):
    def test_capture_review_reload_and_profile_isolation_without_visiting_source_urls(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-source-candidate-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'source-candidates.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                "email": "other@example.invalid"})
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "browser-feed-candidate-employer", "name": "Feed Employer",
                    "domain": "employer.example",
                })
            endpoint = "https://employer.example/jobs.json"
            source = upsert_company_job_source(
                store, company_id="browser-feed-candidate-employer", provider="rss",
                tenant_identifier="browser-feed", public_api_url=endpoint,
                collection_strategy="PUBLIC_JSON",
            )
            import_company_feed_jobs(
                store, company_id="browser-feed-candidate-employer", source_id=source["id"],
                provider="rss", tenant="browser-feed", endpoint=endpoint,
                strategy="PUBLIC_JSON", profile_id="fixture", items=[{
                    "id": "browser-feed-role", "title": "Synthetic Platform Engineer",
                    "description": "Maintain synthetic application systems.",
                    "url": "https://jobs.example-ats.invalid/apply/role-1",
                }],
            )
            SourceUrlProbe.requests = []
            probe = HTTPServer(("127.0.0.1", 0), SourceUrlProbe)
            probe_thread = threading.Thread(target=probe.serve_forever, daemon=True)
            probe_thread.start()
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
                        self.fail("Isolated source-candidate API did not start")
                    time.sleep(0.02)

                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    def execute_rescore(route):
                        response = route.fetch()
                        operation_id = response.json()["operation_id"]
                        with patch.dict("os.environ", MJOBS_DATABASE_URL=store.url,
                                        MJOBS_OPERATION_MODE="FIXTURE",
                                        MJOBS_OUTPUT_DIR=str(root)):
                            from workers.tasks import execute_operation
                            result = execute_operation(operation_id)
                        self.assertEqual(result["status"], "COMPLETED", result)
                        route.fulfill(response=response)

                    context.route("**/*", local_only)
                    context.route("**/matches", execute_rescore)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Capture a job", exact=True).first.click()
                    page.get_by_role("textbox", name="Job title", exact=True).fill("Systems Administrator")
                    page.get_by_role("textbox", name="Company", exact=True).fill("Example Employer")
                    page.get_by_label("Job description", exact=True).fill("Maintain Linux infrastructure.")
                    page.get_by_label("Job reference URL", exact=True).fill(
                        f"http://127.0.0.1:{probe.server_port}/linkedin/job/123?tracking=private-token")
                    page.get_by_label("Application URL", exact=True).fill(
                        "https://www.talenthop.com/apply/role-123?session=private-token#form")
                    page.get_by_label("Reported discovery origin", exact=True).fill("LinkedIn")
                    page.get_by_role("button", name="Save captured job", exact=True).click()
                    expect(page.get_by_role("status").filter(has_text="Job captured and evaluated")).to_be_visible()
                    page.get_by_role("button", name="Sources", exact=True).click()
                    feed_candidate = page.get_by_role(
                        "article", name="Source candidate jobs.example-ats.invalid")
                    expect(feed_candidate).to_contain_text("SUPPORTED COLLECTION")
                    expect(feed_candidate).to_contain_text("https://employer.example/jobs.json")
                    expect(feed_candidate).to_contain_text(
                        "https://jobs.example-ats.invalid/apply/role-1")
                    expect(feed_candidate).to_contain_text("Collection is disabled")
                    candidate = page.get_by_role("article", name="Source candidate talenthop.com")
                    expect(candidate).to_contain_text("LinkedIn")
                    expect(candidate).to_contain_text("https://www.talenthop.com/apply/role-123")
                    expect(candidate).not_to_contain_text("private-token")
                    expect(candidate).to_contain_text("Collection is disabled")
                    candidate.get_by_label("Site type assessment for talenthop.com").select_option("ATS")
                    candidate.get_by_label("Review reason for talenthop.com").fill(
                        "Reported destination is an ATS; endpoint access was denied and collection is not authorized.")
                    candidate.get_by_label("API or feed evidence status for talenthop.com").select_option("BLOCKED")
                    candidate.get_by_label("API or feed evidence reference for talenthop.com").fill(
                        "https://talenthop.com/api/jobs?token=private-token")
                    candidate.get_by_label("API or feed evidence note for talenthop.com").fill(
                        "Prior read-only inspection of the job endpoint was denied.")
                    candidate.get_by_label("Access and terms note for talenthop.com").fill(
                        "Terms and automated access permission remain unverified.")
                    candidate.get_by_label("Capabilities and limits note for talenthop.com").fill(
                        "No documented feed is currently evidenced.")
                    candidate.get_by_label("Expected value and maintenance note for talenthop.com").fill(
                        "Expected value is unknown; ongoing maintenance cannot be assessed.")
                    candidate.get_by_role("button", name="Mark blocked", exact=True).click()
                    expect(candidate).to_contain_text("BLOCKED")
                    expect(candidate).to_contain_text("ATS")
                    expect(candidate).to_contain_text("Reported destination is an ATS")
                    expect(candidate).to_contain_text("Public API/feed access is blocked")
                    expect(candidate).to_contain_text("https://talenthop.com/api/jobs")
                    expect(candidate).to_contain_text("Terms and automated access permission remain unverified.")
                    expect(candidate).not_to_contain_text("private-token")
                    candidate.get_by_text("Review history (1)", exact=True).click()
                    expect(candidate).to_contain_text("Prior read-only inspection of the job endpoint was denied.")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(page.get_by_role(
                        "article", name="Source candidate jobs.example-ats.invalid")).to_be_visible()
                    candidate = page.get_by_role("article", name="Source candidate talenthop.com")
                    expect(candidate).to_contain_text("BLOCKED")
                    expect(candidate).to_contain_text("ATS")
                    expect(candidate).to_contain_text("Public API/feed access is blocked")
                    expect(candidate).to_contain_text("Collection is disabled")
                    page.locator("#active-profile").select_option("other")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(page.get_by_text("No source candidates need review for this profile.", exact=True)).to_be_visible()
                    page.get_by_label("Source site URL").fill(
                        "https://jobs.unknown.invalid/apply?token=private-token")
                    page.get_by_label("Reported discovery origin for source site").fill("LinkedIn")
                    page.get_by_role("button", name="Record source URL", exact=True).click()
                    reported = page.get_by_role("article", name="Source candidate jobs.unknown.invalid")
                    expect(reported).to_contain_text("Discovery origin (user reported): LinkedIn")
                    expect(reported).to_contain_text("Source URL: https://jobs.unknown.invalid/apply")
                    expect(reported).to_contain_text("Application destination: Unknown")
                    expect(reported).not_to_contain_text("private-token")
                    expect(reported).to_contain_text("Linked job: None · Linked application: None")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    reported = page.get_by_role("article", name="Source candidate jobs.unknown.invalid")
                    expect(reported).to_be_visible()
                    expect(reported).to_contain_text("No integration task linked.")
                    reported.get_by_label("Site type assessment for jobs.unknown.invalid").select_option("ATS")
                    reported.get_by_label("Review reason for jobs.unknown.invalid").fill(
                        "Existing provider issue tracks a manual adapter evaluation.")
                    reported.get_by_label("API or feed evidence status for jobs.unknown.invalid").select_option("UNKNOWN")
                    reported.get_by_label("API or feed evidence note for jobs.unknown.invalid").fill(
                        "The integration issue is user supplied; public feed support is not verified here.")
                    reported.get_by_label("Access and terms note for jobs.unknown.invalid").fill(
                        "Access and terms are not verified.")
                    reported.get_by_label("Capabilities and limits note for jobs.unknown.invalid").fill(
                        "No provider capabilities have been verified.")
                    reported.get_by_label("Expected value and maintenance note for jobs.unknown.invalid").fill(
                        "Expected value and maintenance remain unknown.")
                    reported.get_by_label("Integration task issue number for jobs.unknown.invalid").fill("302")
                    reported.get_by_role("button", name="Queue integration review", exact=True).click()
                    expect(reported).to_contain_text("INTEGRATION QUEUED")
                    expect(reported).to_contain_text("Integration issue #302 · user supplied; not externally verified (USER_SUPPLIED_UNVERIFIED)")
                    expect(reported).to_contain_text("Collection is disabled")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    reported = page.get_by_role("article", name="Source candidate jobs.unknown.invalid")
                    expect(reported).to_contain_text("Integration issue #302 · user supplied; not externally verified (USER_SUPPLIED_UNVERIFIED)")
                    expect(reported).to_contain_text("Review history (1)")
                    expect(reported).to_contain_text("Integration issue #302 · user supplied; not externally verified (USER_SUPPLIED_UNVERIFIED)")
                    expect(reported).to_contain_text("Collection is disabled")
                    page.locator("#active-profile").select_option("fixture")
                    expect(page.get_by_role("article", name="Source candidate talenthop.com")).to_be_visible()
                    self.assertEqual(external, [])
                    self.assertEqual(SourceUrlProbe.requests, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                probe.shutdown()
                probe.server_close()
                probe_thread.join(timeout=5)
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
