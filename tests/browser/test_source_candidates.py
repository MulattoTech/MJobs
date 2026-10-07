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
from packages.persistence.store import Store
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
                    page.get_by_label("Job title", exact=True).fill("Systems Administrator")
                    page.get_by_label("Company", exact=True).fill("Example Employer")
                    page.get_by_label("Job description", exact=True).fill("Maintain Linux infrastructure.")
                    page.get_by_label("Job reference URL", exact=True).fill(
                        f"http://127.0.0.1:{probe.server_port}/linkedin/job/123?tracking=private-token")
                    page.get_by_label("Application URL", exact=True).fill(
                        "https://www.talenthop.com/apply/role-123?session=private-token#form")
                    page.get_by_label("Reported discovery origin", exact=True).fill("LinkedIn")
                    page.get_by_role("button", name="Save captured job", exact=True).click()
                    expect(page.get_by_role("status").filter(has_text="Job captured and evaluated")).to_be_visible()
                    page.get_by_role("button", name="Sources", exact=True).click()
                    candidate = page.get_by_role("article", name="Source candidate talenthop.com")
                    expect(candidate).to_contain_text("LinkedIn")
                    expect(candidate).to_contain_text("https://www.talenthop.com/apply/role-123")
                    expect(candidate).not_to_contain_text("private-token")
                    expect(candidate).to_contain_text("Collection disabled")
                    candidate.get_by_label("Site type assessment for talenthop.com").select_option("ATS")
                    candidate.get_by_label("Review reason for talenthop.com").fill(
                        "Reported destination is an ATS; endpoint access was denied and collection is not authorized.")
                    candidate.get_by_role("button", name="Mark blocked", exact=True).click()
                    expect(candidate).to_contain_text("BLOCKED")
                    expect(candidate).to_contain_text("ATS")
                    expect(candidate).to_contain_text("Reported destination is an ATS")
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    candidate = page.get_by_role("article", name="Source candidate talenthop.com")
                    expect(candidate).to_contain_text("BLOCKED")
                    expect(candidate).to_contain_text("ATS")
                    expect(candidate).to_contain_text("Collection disabled")
                    page.locator("#active-profile").select_option("other")
                    page.get_by_role("button", name="Sources", exact=True).click()
                    expect(page.get_by_text("No third-party source candidates are recorded for this profile.", exact=True)).to_be_visible()
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
