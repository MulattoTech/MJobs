"""Connected browser proof for the generic job-match fit-factor display."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE


class EducationMatchExplanationBrowserTests(unittest.TestCase):
    def test_job_detail_renders_persisted_education_fit_factor(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-education-fit-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'education.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = copy.deepcopy(PROFILE)
            profile["facts"].append({
                "id": "approved-bachelors", "kind": "education",
                "text": "Bachelor of Science, North Valley University, 2018.",
                "source": "user-approved education record", "confidence": 95,
                "verified": True, "user_approved": True,
            })
            store.save_profile(profile)
            captured = capture_manual_job(store, {
                "request_id": "browser-education-fit", "title": "Systems Administrator",
                "company": "Example Infrastructure", "description": "Bachelor's degree required.",
                "reference_url": "https://jobs.example.invalid/education/required",
                "profile_id": "fixture",
            })["job"]
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == captured["id"]).values(
                    attributes={**captured["attributes"],
                                "education_requirement": "BACHELORS_REQUIRED",
                                "education_requirement_raw": "Bachelor's degree required."}))
            recalculate(store, "fixture")

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
                        self.fail("Isolated education-match API did not start")
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

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(card).to_be_visible()
                    card.click()
                    detail = page.locator(".job-detail")
                    expect(detail.get_by_role("list", name="Additional fit signals")).to_contain_text(
                        "Education: Bachelor's degree (REQUIRED) — ALIGNS")
                    expect(detail.get_by_role("list", name="Additional fit signals")).to_contain_text(
                        "An approved, sufficiently confident education fact explicitly records a bachelor's degree.")
                    expect(detail).to_contain_text("does not change the score")
                    self.assertEqual(external, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
