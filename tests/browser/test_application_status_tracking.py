"""Connected profile-scoped UI coverage for all required manual statuses."""
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
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_STATUSES = ["APPLIED", "RECRUITER_CONTACT", "INTERVIEW",
                     "OFFER", "REJECTED", "WITHDRAWN"]


def stage_label(status):
    if status == "APPLIED":
        return "Applied — reported by you"
    return " ".join(word.title() for word in status.split("_"))


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class ApplicationStatusTrackingBrowserTests(unittest.TestCase):
    def test_user_sets_all_required_statuses_and_reads_history_after_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-application-status-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'status.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                "email": "other@example.invalid"})
            with fixture_server() as source_origin:
                discover(store, source_origin, "fixture")
            job_id = store.jobs("fixture")[0]["id"]
            job_title = store.job(job_id)["title"]
            application_id = prepare(store, job_id, "fixture", root)

            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            try:
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Application tracking API did not start")
                    time.sleep(0.02)

                errors, external = [], []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.get_by_label("Active profile", exact=True).select_option("fixture")
                    main_nav = page.get_by_role("navigation", name="Main navigation")
                    main_nav.get_by_role("button").filter(has_text="Applications").click()
                    pipeline = page.get_by_role("region", name="Application pipeline")
                    not_tracked = pipeline.get_by_role("region", name="Not tracked", exact=True)
                    card = not_tracked.get_by_role("button", name=job_title, exact=False)
                    expect(card).to_be_visible()
                    card.click()

                    tracking = page.get_by_role("region", name="Application tracking")
                    expect(tracking).to_be_visible()
                    status_select = tracking.get_by_label("Manual application status", exact=True)
                    available_statuses = status_select.locator("option").evaluate_all(
                        "options => options.map(option => option.value)")
                    self.assertTrue(set(REQUIRED_STATUSES).issubset(set(available_statuses)),
                                    available_statuses)
                    update_status = tracking.get_by_role("button", name="Update status", exact=True)
                    endpoint = (origin + f"/profiles/fixture/applications/{application_id}"
                                "/tracking/status")
                    for status in REQUIRED_STATUSES:
                        status_select.select_option(status)
                        expect(status_select).to_have_value(status)
                        expect(update_status).to_be_enabled()
                        with page.expect_response(lambda response: response.url == endpoint
                                                  and response.request.method == "PUT") as result:
                            update_status.click()
                        payload = result.value.json()
                        self.assertTrue(result.value.ok, payload)
                        self.assertEqual(payload["status"], status)
                        self.assertEqual(payload["attribution"], "USER_REPORTED")
                        expect(tracking.get_by_role("list", name="Application history")).to_contain_text(
                            stage_label(status))

                    persisted = context.request.get(
                        origin + f"/profiles/fixture/applications/{application_id}/tracking").json()
                    self.assertEqual(persisted["status"], "WITHDRAWN")
                    self.assertEqual([event["status"] for event in persisted["history"]
                                      if event["type"] == "status"], REQUIRED_STATUSES)
                    application = store.application(application_id)
                    self.assertIsNone(application["confirmation"])
                    self.assertNotEqual(application["state"], "APPLIED")
                    self.assertFalse(context.request.get(origin + "/health").json()[
                        "external_submission_enabled"])

                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    main_nav.get_by_role("button").filter(has_text="Applications").click()
                    withdrawn = page.get_by_role("region", name="Withdrawn", exact=True)
                    withdrawn.get_by_role("button", name=job_title, exact=False).click()
                    tracking = page.get_by_role("region", name="Application tracking")
                    history = tracking.get_by_role("list", name="Application history")
                    for status in REQUIRED_STATUSES:
                        expect(history).to_contain_text(stage_label(status))
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Application tracking API must stop")


if __name__ == "__main__":
    unittest.main()
