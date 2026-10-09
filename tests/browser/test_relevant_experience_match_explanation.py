"""Connected browser proof for approved, score-neutral experience fit evidence."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE


class RelevantExperienceMatchExplanationBrowserTests(unittest.TestCase):
    def test_job_detail_renders_only_approved_experience_evidence_without_score_change(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-experience-fit-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'experience.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = copy.deepcopy(PROFILE)
            profile["facts"].append({
                "id": "approved-experience", "kind": "technology_experience",
                "text": "Administered Active Directory and Windows Server for enterprise users",
                "source": "user-approved resume revision", "confidence": 95,
                "verified": True, "user_approved": True,
            })
            profile["facts"].append({
                "id": "pending-experience", "kind": "technology_experience",
                "text": "Managed VMware clusters", "source": "pending import", "confidence": 100,
                "verified": False, "user_approved": False,
            })
            store.save_profile(profile)
            job = capture_manual_job(store, {
                "request_id": "browser-relevant-experience-fit", "title": "Systems Administrator",
                "company": "Example Infrastructure", "description": "Windows Server and Active Directory",
                "reference_url": "https://jobs.example.invalid/experience-fit/role",
                "profile_id": "fixture",
            })["job"]
            recalculate(store, "fixture")
            api_match = store.job(job["id"], profile_id="fixture")["match"]
            factor = next(row for row in api_match["components"]["fit_factors"]
                          if row["id"] == "relevant_experience")
            self.assertEqual(api_match["score"], 0)
            self.assertEqual(factor["status"], "ALIGNS")
            self.assertEqual(factor["score_effect"], "NONE")

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
                        self.fail("Isolated relevant-experience API did not start")
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
                    factors = detail.get_by_role("list", name="Additional fit signals")
                    expect(factors).to_contain_text("Relevant experience — ALIGNS")
                    expect(factors).to_contain_text(
                        "Approved experience facts directly mention locally recognized job skills: Active Directory, Windows Server.")
                    expect(factors).not_to_contain_text("Managed VMware clusters")
                    expect(detail.locator(".big-score")).to_contain_text("0%")
                    expect(factors).to_contain_text("This does not change the score")
                    self.assertEqual(external, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
