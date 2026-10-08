"""Connected browser proof for persisted-score match categories."""
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
class MatchCategoriesBrowserTests(unittest.TestCase):
    def test_job_cards_and_detail_follow_profile_score_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-match-categories-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'categories.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            salary_profile = copy.deepcopy(PROFILE)
            salary_profile["preferences"] = {
                **salary_profile.get("preferences", {}),
                "salary_expectations": "USD 90000-110000 annually",
            }
            store.save_profile(salary_profile)
            second_profile = copy.deepcopy(PROFILE)
            second_profile.update(id="second-profile", name="Second Profile",
                                  email="second@example.test")
            store.save_profile(second_profile)

            scored = capture_manual_job(store, {
                "request_id": "match-category-scored",
                "title": "Synthetic scored role",
                "company": "Synthetic Company",
                "description": "Windows Server and Active Directory support.",
                "reference_url": "https://jobs.example.test/scored",
                "application_url": "https://apply.example.test/scored",
                "source_details": {},
            })["job"]
            unscored = capture_manual_job(store, {
                "request_id": "match-category-unscored",
                "title": "Synthetic unscored role",
                "company": "Synthetic Company",
                "description": "No match has been calculated for this posting.",
                "reference_url": "https://jobs.example.test/unscored",
                "application_url": "https://apply.example.test/unscored",
                "source_details": {},
            })["job"]
            with store.engine.begin() as connection:
                connection.execute(m.jobs.update().where(m.jobs.c.id == scored["id"]).values(
                    attributes={**scored["attributes"], "salary_min": 100000,
                                "salary_max": 120000, "salary_period": "YEAR",
                                "salary_currency": "USD", "salary_source": "EMPLOYER_PROVIDED"}
                ))
            recalculate(store, "fixture")
            recalculate(store, "second-profile")
            with store.engine.begin() as connection:
                for profile_id, score in (("fixture", 94), ("second-profile", 95)):
                    connection.execute(m.matches.update().where(
                        m.matches.c.job_id == scored["id"],
                        m.matches.c.profile_id == profile_id,
                    ).values(score=score))
                connection.execute(m.matches.delete().where(
                    m.matches.c.job_id == unscored["id"],
                ))

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
                        self.fail("Isolated match-categories API did not start")
                    time.sleep(0.02)

                external = []
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()

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
                    expect(page.get_by_text("External submission OFF", exact=True)).to_be_visible()

                    scored_card = page.locator(".job-card").filter(
                        has_text="Synthetic scored role")
                    unscored_card = page.locator(".job-card").filter(
                        has_text="Synthetic unscored role")
                    expect(scored_card).to_contain_text("94%")
                    expect(scored_card).to_contain_text("Excellent")
                    expect(unscored_card).to_contain_text("Unscored")
                    for category in ("Exceptional", "Excellent", "Strong", "Possible", "Weak", "Poor"):
                        expect(unscored_card).not_to_contain_text(category)
                    unscored_card.click()
                    detail = page.locator(".job-detail")
                    expect(detail).to_contain_text("Unscored")
                    for category in ("Exceptional", "Excellent", "Strong", "Possible", "Weak", "Poor"):
                        expect(detail).not_to_contain_text(category)
                    scored_card.click()
                    expect(detail.locator(".big-score")).to_contain_text("94%")
                    expect(detail.locator(".big-score")).to_contain_text("Excellent")
                    expect(detail.get_by_label("Additional fit signals")).to_contain_text(
                        "Salary expectation — ALIGNS")

                    page.get_by_label("Active profile", exact=True).select_option(
                        "second-profile")
                    expect(scored_card).to_contain_text("95%")
                    expect(scored_card).to_contain_text("Exceptional")
                    scored_card.click()
                    expect(detail.locator(".big-score")).to_contain_text("95%")
                    expect(detail.locator(".big-score")).to_contain_text("Exceptional")
                    expect(detail.get_by_label("Additional fit signals")).to_contain_text(
                        "Salary expectation — UNKNOWN")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value(
                        "second-profile")
                    expect(scored_card).to_contain_text("95%")
                    expect(scored_card).to_contain_text("Exceptional")
                    scored_card.click()
                    expect(page.locator(".job-detail .big-score")).to_contain_text("95%")
                    expect(page.locator(".job-detail .big-score")).to_contain_text(
                        "Exceptional")

                    self.assertEqual(store.job(scored["id"], "fixture")["match"]["score"], 94)
                    self.assertEqual(
                        store.job(scored["id"], "second-profile")["match"]["score"], 95)
                    self.assertIsNone(store.job(unscored["id"], "fixture")["match"])
                    self.assertEqual(external, [])
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                store.engine.dispose()
