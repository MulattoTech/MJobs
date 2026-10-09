"""Connected browser proof for score-neutral remote-location explanations."""
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
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class WorkLocationMatchExplanationBrowserTests(unittest.TestCase):
    def test_job_detail_explains_remote_region_and_profile_scopes_without_location_leak(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-work-location-fit-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'work-location.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = copy.deepcopy(PROFILE)
            profile["contact"] = {"country": "United States", "state": "Oklahoma"}
            other = copy.deepcopy(PROFILE)
            other.update({"id": "other", "name": "Other Profile", "email": "other@example.test"})
            other["contact"] = {"country": "Canada", "state": "Ontario"}
            store.save_profile(profile)
            store.save_profile(other)
            job = capture_manual_job(store, {
                "request_id": "browser-work-location-fit", "title": "Systems Administrator",
                "company": "Example Infrastructure",
                "description": "Python and SQL administration. Remote role limited to Oklahoma and Texas.",
                "location": "Remote, United States", "remote_type": "REMOTE_SPECIFIC_STATES",
                "reference_url": "https://jobs.example.invalid/work-location-fit/role",
                "profile_id": "fixture",
            })["job"]
            with store.engine.begin() as connection:
                connection.execute(m.jobs.update().where(m.jobs.c.id == job["id"]).values(
                    remote_type="REMOTE_SPECIFIC_STATES",
                    attributes={**job["attributes"], "remote_regions": ["OK", "TX"]},
                ))
            recalculate(store, "fixture")
            recalculate(store, "other")
            for profile_id, expected in (("fixture", "ALIGNS"), ("other", "NO_DIRECT_MATCH")):
                match = store.job(job["id"], profile_id=profile_id)["match"]
                factor = next(row for row in match["components"]["fit_factors"]
                              if row["id"] == "remote_location")
                self.assertEqual(match["score"], 100)
                self.assertEqual(factor["status"], expected)
                self.assertEqual(factor["score_effect"], "NONE")

            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external = []
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated location-matching API did not start")
                    time.sleep(0.02)

                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_init_script("localStorage.setItem('mjobs-profile', 'fixture')")

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
                    card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(card).to_be_visible()
                    card.click()
                    detail = page.locator(".job-detail")
                    factors = detail.get_by_role("list", name="Additional fit signals")
                    expect(factors).to_contain_text("Remote work region — ALIGNS")
                    expect(factors).to_contain_text("within the job's stated remote-work regions")
                    expect(factors).not_to_contain_text("Oklahoma")
                    expect(factors).not_to_contain_text("Ontario")
                    expect(detail.locator(".big-score")).to_contain_text("100%")
                    page.locator("#active-profile").select_option("other")
                    other_card = page.locator(".job-card").filter(has_text="Systems Administrator")
                    expect(other_card).to_be_visible()
                    other_card.click()
                    other_factors = page.locator(".job-detail").get_by_role(
                        "list", name="Additional fit signals",
                    )
                    expect(other_factors).to_contain_text(
                        "Remote work region — NO DIRECT MATCH",
                    )
                    expect(page.locator(".job-detail .big-score")).to_contain_text("100%")
                    expect(other_factors).to_contain_text("does not reduce the score")
                    self.assertEqual(external, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
