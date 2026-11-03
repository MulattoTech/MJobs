"""Chromium reads the AI wording, original sources and saved document bytes."""
import threading
import time
import unittest
from urllib.parse import urlsplit

import uvicorn
from playwright.sync_api import expect, sync_playwright

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration import test_ai_resume_fact_trace as contract


class AIResumeFactTraceBrowserTests(unittest.TestCase):
    def test_reviewed_ai_wording_download_and_historical_sources(self):
        case = contract.AIResumeTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        draft = case.request()
        accepted = case.accept(draft)
        case.store.engine.dispose()
        case.store = Store(case.url)
        changed = [
            {**row, "text": "Later current-profile source canary."}
            if row["id"] == "experience" else row
            for row in case.profile_data["facts"]
        ]
        case.store.save_profile({**case.profile_data, "facts": changed})
        app = create_app(case.store, "http://127.0.0.1:1", case.output,
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
                    self.fail("AI resume browser fixture did not start")
                time.sleep(0.02)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(accept_downloads=True)
                external = []

                def local_only(route):
                    if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                        external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", local_only)
                page = context.new_page()
                page.goto(origin)
                page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                page.reload()
                page.get_by_role("button", name="Applications", exact=False).first.click()
                page.locator(".job-card").filter(has_text="Systems Engineer").first.click()
                resume = page.get_by_role("region", name="resume version 1", exact=True)
                resume.get_by_text("Generation details and facts", exact=True).click()
                expect(resume.get_by_role("region", name="AI-assisted resume wording")).to_be_visible()
                expect(resume).to_contain_text("openai-compatible")
                expect(resume).to_contain_text("fixture-model")
                expect(resume).to_contain_text(contract.SOURCE)
                expect(resume).to_contain_text(contract.PROPOSAL)
                expect(resume).to_contain_text("Source: synthetic-approved-record")
                expect(resume).not_to_contain_text("Later current-profile source canary")
                expect(resume).not_to_contain_text("Unapproved secret")
                with page.expect_download() as observed:
                    resume.get_by_role("link", name="Download TXT", exact=True).click()
                downloaded = observed.value
                self.assertIsNone(downloaded.failure())
                target = case.root / "downloaded-resume.txt"
                downloaded.save_as(target)
                self.assertIn(contract.PROPOSAL, target.read_text(encoding="utf-8"))
                letter = page.get_by_role("region", name="cover-letter version 1", exact=True)
                expect(letter).not_to_contain_text("AI-assisted resume wording")
                self.assertFalse(external)
                self.assertEqual(len(case.provider.calls), 1)
                self.assertEqual(case.store.application(accepted["application_id"])["state"], "PREPARED")
                context.close()
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            self.assertFalse(thread.is_alive())
