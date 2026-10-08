"""Connected browser proof for manually captured qualification section strengths."""
import copy
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


class ManualRequirementSectionStrengthBrowserTests(unittest.TestCase):
    def test_manual_capture_displays_required_and_preferred_section_strengths(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-section-strength-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'trial.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(copy.deepcopy(PROFILE))
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
                    outside_origin = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            outside_origin.append(route.request.url)
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
                    expect(page.get_by_text("External submission OFF", exact=True)).to_be_visible()
                    page.get_by_role("button", name="Capture a job", exact=True).first.click()
                    page.get_by_role("textbox", name="Job title", exact=True).fill("Systems Administrator")
                    page.get_by_role("textbox", name="Company", exact=True).fill("Example Infrastructure")
                    page.get_by_label("Job description", exact=True).fill(
                        "Required Qualifications:\nWindows Server\n"
                        "Preferred Qualifications:\nActive Directory")
                    page.get_by_label("Job reference URL", exact=True).fill(
                        "https://jobs.example.invalid/qualification-sections/1")
                    with page.expect_response(
                        lambda response: response.url == origin + "/jobs/manual"
                        and response.request.method == "POST"
                    ) as captured:
                        page.get_by_role("button", name="Save captured job", exact=True).click()
                    self.assertEqual(captured.value.status, 201)
                    page.locator(".job-card").filter(has_text="Systems Administrator").click()
                    strengths = page.locator(".job-detail").get_by_role(
                        "list", name="Skill match explanation")
                    expect(strengths).to_contain_text("Windows Server — REQUIRED")
                    expect(strengths).to_contain_text("Active Directory — PREFERRED")
                    self.assertEqual(outside_origin, [])
                    self.assertEqual(store.count("application_attempts"), 0)
                    page.close()
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
