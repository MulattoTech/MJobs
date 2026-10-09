"""Connected Chromium recovery and reload acceptance for the skill-gap heatmap."""
from pathlib import Path
import re
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
from services.documents.generate import prepare
from services.tracking.applications import set_application_pipeline_status
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
HEATMAP_PATH = "/profiles/fixture/skill-gap-heatmap"


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SkillGapHeatmapReloadRecoveryBrowserTests(unittest.TestCase):
    def test_transient_heatmap_failure_retries_and_reads_same_result_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-skill-gap-recovery-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'heatmap.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(PROFILE)
            capture_manual_job(store, {
                "request_id": "heatmap-retry-python",
                "title": "Synthetic Python support role",
                "company": "Synthetic Heatmap Employer",
                "description": "Responsibilities: Support users. Python is required.",
                "reference_url": "https://jobs.example.test/heatmap-retry-python",
                "profile_id": "fixture",
            })
            aws_job = capture_manual_job(store, {
                "request_id": "heatmap-interview-aws",
                "title": "Synthetic AWS infrastructure role",
                "company": "Synthetic Interview Employer",
                "description": "Required Qualifications:\nAWS\nPython\nResponsibilities:\nOperate synthetic infrastructure.",
                "reference_url": "https://jobs.example.test/heatmap-interview-aws",
                "profile_id": "fixture",
            })["job"]
            linux_job = capture_manual_job(store, {
                "request_id": "heatmap-applied-linux",
                "title": "Synthetic Linux operations role",
                "company": "Synthetic Application Employer",
                "description": "Required Qualifications:\nLinux\nPython\nResponsibilities:\nOperate synthetic systems.",
                "reference_url": "https://jobs.example.test/heatmap-applied-linux",
                "profile_id": "fixture",
            })["job"]
            capture_manual_job(store, {
                "request_id": "heatmap-unreported-azure",
                "title": "Synthetic Azure platform role",
                "company": "Synthetic Unreported Employer",
                "description": "Required Qualifications:\nAzure\nResponsibilities:\nOperate synthetic systems.",
                "reference_url": "https://jobs.example.test/heatmap-unreported-azure",
                "profile_id": "fixture",
            })
            recalculate(store, "fixture")
            interview_application = prepare(store, aws_job["id"], "fixture", root)
            set_application_pipeline_status(
                store, "fixture", interview_application, "INTERVIEW", "heatmap-interview"
            )
            applied_application = prepare(store, linux_job["id"], "fixture", root)
            set_application_pipeline_status(
                store, "fixture", applied_application, "APPLIED", "heatmap-applied"
            )

            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run,
                                      kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated skill-gap heatmap API did not start")
                    time.sleep(0.02)

                external = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    heatmap_requests = 0

                    def local_only(route):
                        nonlocal heatmap_requests
                        request_url = route.request.url
                        if urlsplit(request_url).netloc != urlsplit(origin).netloc:
                            external.append(request_url)
                            route.abort()
                        elif urlsplit(request_url).path == HEATMAP_PATH:
                            heatmap_requests += 1
                            if heatmap_requests == 1:
                                route.fulfill(status=503, content_type="application/json",
                                              body='{"detail":"temporary test failure"}')
                            else:
                                route.continue_()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + HEATMAP_PATH)
                            and response.request.method == "GET") as failed_response:
                        page.get_by_role("button", name=re.compile(r"Learning|Matching", re.I)).click()
                    self.assertEqual(failed_response.value.status, 503)
                    heatmap = page.get_by_role("region", name="Skill gap heatmap", exact=True)
                    expect(heatmap.get_by_role("alert")).to_be_visible()
                    retry = heatmap.get_by_role("button", name="Retry", exact=True)
                    expect(retry).to_be_visible()

                    with page.expect_response(lambda response:
                            response.url.startswith(origin + HEATMAP_PATH)
                            and response.request.method == "GET") as retried_response:
                        retry.click()
                    self.assertEqual(retried_response.value.status, 200)
                    payload = retried_response.value.json()
                    self.assertEqual(payload["profile_id"], "fixture")
                    self.assertEqual(payload["jobs_examined"], 4)
                    self.assertEqual(
                        [row["skill_id"] for row in payload["skills"][:3]],
                        ["AWS", "LINUX", "AZURE"],
                    )
                    by_skill = {row["skill_id"]: row for row in payload["skills"]}
                    self.assertEqual(by_skill["AWS"]["no_evidence_interview_job_count"], 1)
                    self.assertEqual(by_skill["LINUX"]["no_evidence_applied_job_count"], 1)
                    self.assertEqual(by_skill["AZURE"]["no_evidence_applied_job_count"], 0)
                    expect(heatmap).to_contain_text("AWS")
                    expect(heatmap).to_contain_text("Linux")
                    expect(heatmap).to_contain_text("Azure")
                    expect(heatmap).to_contain_text("interview-reported gaps, applied-reported gaps")
                    expect(heatmap).to_contain_text(
                        "It does not confirm that you lack the skill.")

                    # Reload creates a fresh browser document and reads the persisted match again.
                    page.reload(wait_until="domcontentloaded")
                    with page.expect_response(lambda response:
                            response.url.startswith(origin + HEATMAP_PATH)
                            and response.request.method == "GET") as reloaded_response:
                        page.get_by_role("button", name=re.compile(r"Learning|Matching", re.I)).click()
                    self.assertEqual(reloaded_response.value.status, 200)
                    self.assertEqual(reloaded_response.value.json(), payload)
                    expect(page.get_by_role("region", name="Skill gap heatmap", exact=True)).to_contain_text(
                        "Python")
                    self.assertEqual(heatmap_requests, 3)
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
