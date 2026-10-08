"""Connected browser acceptance for sensitive profile suggestions and review gates."""
from copy import deepcopy
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


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(),
                     "Build apps/web before browser acceptance")
class SensitiveAnswerPolicyBrowserTests(unittest.TestCase):
    def test_policy_suggestions_are_attributed_optional_and_export_only_after_review(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-sensitive-policy-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            profile = deepcopy(PROFILE)
            profile["preferences"] = {
                "sensitive_answer_policies": {
                    "RACE_ETHNICITY": {"mode": "PREFER_NOT_TO_ANSWER"},
                    "SEX_GENDER": {
                        "mode": "USE_CONFIGURED_RESPONSE",
                        "response": "SYNTHETIC_USER_CONFIGURED_RESPONSE_6f96",
                    },
                },
            }
            store.save_profile(profile)
            other = deepcopy(PROFILE)
            other.update(id="other-profile", name="Other Fixture", email="other@example.test")
            store.save_profile(other)

            with fixture_server() as fixture:
                discover(store, fixture, "fixture")
                job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
                application_id = prepare(store, job["id"], "fixture", root)
                sock = bound_browser_socket()
                origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
                server = uvicorn.Server(uvicorn.Config(
                    create_app(store, fixture, root, operation_mode="FIXTURE"),
                    log_level="error", access_log=False,
                ))
                thread = threading.Thread(
                    target=server.run, kwargs={"sockets": [sock]}, daemon=True)
                thread.start()
                deadline = time.monotonic() + 10
                methods, outside, errors = [], [], []
                before_documents = store.count("documents")
                before_attempts = store.count("application_attempts")
                try:
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            self.fail("Isolated sensitive-answer application did not start")
                        time.sleep(0.02)

                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context(viewport={"width": 1280, "height": 900})

                        def local_only(route):
                            methods.append((route.request.method, route.request.url))
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                outside.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.set_default_timeout(8000)
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(origin, wait_until="domcontentloaded")
                        page.locator("#active-profile").select_option("fixture")
                        page.get_by_role("button", name="Applications", exact=True).click()
                        page.get_by_role("region", name="Not tracked").get_by_role(
                            "button", name=job["title"], exact=False).click()

                        answers = page.get_by_role("region", name="Application answers")
                        answers.get_by_role("button", name="Add question", exact=True).click()
                        race = answers.get_by_role("group", name="Question 1")
                        race.get_by_label("Employer question", exact=True).fill(
                            "What is your race/ethnicity?")
                        expect(race).to_contain_text("Profile preference")
                        expect(race).to_contain_text("Prefer not to answer")
                        expect(race).to_contain_text("this profile's explicit policy")
                        expect(race).to_contain_text("requires review")
                        expect(race.get_by_label("Answer", exact=True)).to_have_value("")
                        race.get_by_role("button", name="Use “Prefer not to answer” for this application",
                                          exact=True).click()
                        expect(race.get_by_label("Answer", exact=True)).to_have_value(
                            "Prefer not to answer")
                        expect(race.get_by_role("checkbox", name=
                               "I reviewed this exact answer for this job")).not_to_be_checked()
                        expect(race.get_by_role("button", name="Save to Answer Bank")).to_have_count(0)

                        answers.get_by_role("button", name="Add question", exact=True).click()
                        gender = answers.get_by_role("group", name="Question 2")
                        gender.get_by_label("Employer question", exact=True).fill(
                            "What is your gender identity?")
                        expect(gender).to_contain_text("Profile preference")
                        expect(gender).to_contain_text(
                            "SYNTHETIC_USER_CONFIGURED_RESPONSE_6f96")
                        expect(gender.get_by_label("Answer", exact=True)).to_have_value("")
                        gender.get_by_role("button", name="Use configured response for this application",
                                           exact=True).click()
                        expect(gender.get_by_label("Answer", exact=True)).to_have_value(
                            "SYNTHETIC_USER_CONFIGURED_RESPONSE_6f96")
                        expect(gender.get_by_role("checkbox", name=
                               "I reviewed this exact answer for this job")).not_to_be_checked()
                        expect(gender.get_by_role("button", name="Save to Answer Bank")).to_have_count(0)

                        answers.get_by_role("button", name="Add question", exact=True).click()
                        unknown = answers.get_by_role("group", name="Question 3")
                        unknown.get_by_label("Employer question", exact=True).fill(
                            "How would you describe the experiences that shaped you?")
                        expect(unknown.get_by_label("Answer", exact=True)).to_have_value("")
                        expect(unknown.get_by_role("checkbox", name=
                               "I reviewed this exact answer for this job")).not_to_be_checked()
                        expect(unknown.get_by_role(
                            "button", name="Use “Prefer not to answer” for this application")).to_have_count(0)
                        expect(unknown.get_by_role(
                            "button", name="Use configured response for this application")).to_have_count(0)
                        export = answers.get_by_role(
                            "button", name="Save reviewed answers and export TXT", exact=True)
                        expect(export).to_be_disabled()

                        unknown.get_by_role("button", name="Remove question", exact=True).click()
                        race.get_by_role("checkbox", name=
                            "I reviewed this exact answer for this job").check()
                        gender.get_by_role("checkbox", name=
                            "I reviewed this exact answer for this job").check()
                        expect(export).to_be_enabled()
                        export.click()
                        expect(page.get_by_role("status").filter(
                            has_text="Reviewed answer export saved as document")).to_be_visible()

                        stored = store.application(application_id)
                        self.assertEqual(stored["profile_id"], "fixture")
                        self.assertEqual(store.count("documents"), before_documents + 1)
                        self.assertEqual(store.count("application_attempts"), before_attempts)
                        self.assertFalse(any(
                            method == "POST" and "/mock-run" in url for method, url in methods), methods)
                        self.assertFalse(any(
                            method == "POST" and urlsplit(url).path == "/applications"
                            for method, url in methods), methods)
                        self.assertEqual(outside, [])
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(timeout=10)
                    sock.close()
                    store.engine.dispose()
                self.assertFalse(thread.is_alive(), "Isolated sensitive-answer server must stop")


if __name__ == "__main__":
    unittest.main()
