"""Connected browser proof for explicit, durable email-status review."""
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
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.documents.generate import prepare
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE
from services.tracking.applications import read_application_tracking


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class EmailStatusBrowserTests(unittest.TestCase):
    def test_reviewed_email_status_is_local_explicit_and_survives_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-email-status-browser-") as directory:
            root = Path(directory)
            database = root / "email-status.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                "email": "other@example.test"})
            with fixture_server() as fixture_origin:
                discover(store, fixture_origin, "fixture")
            job = store.jobs("fixture")[0]
            application_id = prepare(store, job["id"], "fixture", root)
            before = store.application(application_id)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(store, "http://127.0.0.1:1", root,
                             "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            external_requests = []
            page_errors = []
            private_values = (
                "PRIVATE_EMAIL_BROWSER_SUBJECT",
                "PRIVATE_EMAIL_BROWSER_ADDRESS@example.test",
                "PRIVATE_EMAIL_BROWSER_BODY",
            )
            try:
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Isolated email-status API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            path = urlsplit(route.request.url).path.lower()
                            if path.endswith(("/submit", "/mock-run")) or (
                                    path.endswith("/apply") and "/email-status/" not in path):
                                external_requests.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name=re.compile(r"^Applications(?:\s|$)")
                    ).click()
                    pipeline = page.get_by_role("region", name="Application pipeline", exact=True)
                    application_card = pipeline.get_by_role(
                        "button", name=re.compile(re.escape(job["title"])))
                    expect(application_card).to_be_visible()
                    application_card.click()

                    review = page.get_by_role("region", name="Review email status", exact=True)
                    expect(review.get_by_role("heading", name="Review email status", exact=True)).to_be_visible()
                    draft = review.get_by_label("Paste email text", exact=True)
                    message = (
                        f"From: {private_values[1]}\nSubject: {private_values[0]}\n\n"
                        "Thank you for applying. We have received your application.\n"
                        + private_values[2]
                    )
                    draft.fill(message)
                    preview_path = (
                        f"/profiles/fixture/applications/{application_id}/email-status/preview")
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == preview_path) as preview_response:
                        review.get_by_role("button", name="Preview message", exact=True).click()
                    self.assertEqual(preview_response.value.status, 200,
                                     preview_response.value.text())
                    preview = preview_response.value.json()
                    self.assertEqual(preview["suggested_status"], "APPLIED")
                    self.assertEqual(preview["expected_tracking_sequence"], 0)
                    preview_panel = page.get_by_role(
                        "region", name="Email status preview", exact=True)
                    expect(preview_panel).to_contain_text("Application confirmation")
                    expect(preview_panel).to_contain_text("Application confirmed")
                    expect(draft).to_have_value(message)
                    for private_value in private_values:
                        expect(preview_panel).not_to_contain_text(private_value)
                        self.assertNotIn(private_value, preview_response.value.text())

                    review.get_by_label(
                        "I reviewed this email and want to record the suggested status.",
                        exact=True,
                    ).check()
                    apply_path = (
                        f"/profiles/fixture/applications/{application_id}/email-status/"
                        f"{preview['preview_id']}/apply")
                    with page.expect_response(lambda response:
                            urlsplit(response.url).path == apply_path) as applied_response:
                        review.get_by_role(
                            "button", name="Confirm reported status", exact=True).click()
                    self.assertEqual(applied_response.value.status, 200,
                                     applied_response.value.text())
                    history = review.get_by_role(
                        "region", name="Reported email status history", exact=True
                    )
                    expect(history).to_contain_text("Reported by you from email")
                    expect(history).to_contain_text("Application confirmed")
                    expect(draft).to_have_value("")
                    for private_value in private_values:
                        expect(history).not_to_contain_text(private_value)
                        self.assertNotIn(private_value, applied_response.value.text())
                        expect(page.locator("body")).not_to_contain_text(private_value)

                    tracking = read_application_tracking(store, "fixture", application_id)
                    self.assertEqual(tracking["status"], "APPLIED")
                    self.assertEqual(tracking["attribution"], "USER_REPORTED_EMAIL")
                    after = store.application(application_id)
                    self.assertEqual(after["state"], before["state"])
                    self.assertEqual(after["confirmation"], before["confirmation"])

                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_label("Active profile", exact=True)).to_have_value("fixture")
                    page.get_by_role("navigation", name="Main navigation").get_by_role(
                        "button", name=re.compile(r"^Applications(?:\s|$)")
                    ).click()
                    pipeline = page.get_by_role("region", name="Application pipeline", exact=True)
                    pipeline.get_by_role(
                        "button", name=re.compile(re.escape(job["title"]))).click()
                    reloaded_review = page.get_by_role(
                        "region", name="Review email status", exact=True)
                    history = reloaded_review.get_by_role(
                        "region", name="Reported email status history", exact=True)
                    expect(history).to_contain_text("Application confirmed")
                    expect(history).to_contain_text("Reported by you from email")
                    for private_value in private_values:
                        expect(page.locator("body")).not_to_contain_text(private_value)
                    other_history = page.request.get(
                        origin + f"/profiles/other/applications/{application_id}/email-status")
                    self.assertEqual(other_history.status, 404)
                    self.assertEqual(external_requests, [])
                    self.assertEqual(page_errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Email-status API server did not stop")


if __name__ == "__main__":
    unittest.main()
