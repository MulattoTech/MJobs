from pathlib import Path
from datetime import datetime
import tempfile
import unittest
import json
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select, update
from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.mock_adapter import apply_mock
from services.applications.form_analysis import analyze_form_page
from services.applications.preflight import build_preflight_result
from services.discovery.pipeline import discover, MockClient
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class MockApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def prepared(self, origin, profile=None):
        if profile is not None:
            self.store.save_profile(profile)
        discover(self.store, origin, "fixture")
        job = next(row for row in self.store.jobs() if row["requisition"] == "B")
        return prepare(self.store, job["id"], "fixture", self.root)

    def history(self, profile_id="fixture", store=None):
        store = store or self.store
        with TestClient(create_app(store, "http://127.0.0.1:1", self.root,
                                   operation_mode="FIXTURE")) as client:
            response = client.get(f"/profiles/{profile_id}/audit-history")
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["events"]

    def test_read_only_form_analysis_extracts_synthetic_dom_without_interaction_or_values(self):
        from playwright.sync_api import sync_playwright

        html = """<!doctype html><html><body><form>
          <section><h2></h2>
            <fieldset><legend></legend>
              <label for="person-name"></label>
              <input id="person-name" name="full_name" type="text"
                     placeholder="Enter your name" aria-label="Applicant name"
                     value="PRIVATE_CURRENT_VALUE">
              <p>Use the name on your resume</p>
              <label for="country"></label>
              <select id="country" name="country">
                <option value="private-us">United States</option>
                <option value="private-ca">Canada</option>
              </select>
              <label for="ssn"></label>
              <input id="ssn" name="ssn" type="text" aria-label="Social Security number" value="PRIVATE_SSN_VALUE">
              <label for="password"></label>
              <input id="password" name="password" type="password" value="PRIVATE_PASSWORD_VALUE">
            </fieldset>
          </section>
          <button type="submit">Submit</button>
        </form><script>
          window.formEvents = 0;
          for (const eventName of ['click', 'input', 'change', 'submit']) {
            document.addEventListener(eventName, () => window.formEvents++, true);
          }
        </script></body></html>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                result = analyze_form_page(page)
                self.assertEqual(result["status"], "PASS")
                by_id = {field["id"]: field for field in result["fields"]}
                person = by_id["person-name"]
                self.assertEqual(person["labels"], [])
                self.assertEqual(person["name"], "full_name")
                self.assertEqual(person["placeholder"], "Enter your name")
                self.assertEqual(person["aria_label"], "Applicant name")
                self.assertEqual(person["input_type"], "text")
                self.assertIsNone(person["nearby_text"])
                self.assertIsNone(person["section_heading"])
                self.assertIsNone(person["fieldset_legend"])
                self.assertEqual(by_id["country"]["options"], ["United States", "Canada"])
                self.assertEqual(by_id["ssn"]["sensitivity"], "SENSITIVE")
                self.assertEqual(by_id["password"]["sensitivity"], "CREDENTIAL")
                serialized = json.dumps(result)
                for private_value in ("PRIVATE_CURRENT_VALUE", "PRIVATE_SSN_VALUE",
                                      "PRIVATE_PASSWORD_VALUE", "private-us", "private-ca"):
                    self.assertNotIn(private_value, serialized)
                self.assertEqual(page.evaluate("window.formEvents"), 0)
            finally:
                browser.close()

    def test_read_only_form_analysis_does_not_capture_rendered_sibling_answers(self):
        from playwright.sync_api import sync_playwright

        html = """<!doctype html><html><body><form>
          <div><input id="span-answer" name="span_answer">
               <span>PRIVATE_SIBLING_SPAN_ANSWER</span></div>
          <div><input id="output-answer" name="output_answer">
               <output>PRIVATE_SIBLING_OUTPUT_ANSWER</output></div>
        </form></body></html>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                result = analyze_form_page(page)
                self.assertEqual(result["status"], "PASS")
                serialized = json.dumps(result)
                for rendered_answer in (
                        "PRIVATE_SIBLING_SPAN_ANSWER", "PRIVATE_SIBLING_OUTPUT_ANSWER"):
                    self.assertNotIn(rendered_answer, serialized)
            finally:
                browser.close()

    def test_read_only_form_analysis_fails_closed_on_dynamic_label_text(self):
        from playwright.sync_api import sync_playwright

        html = """<!doctype html><html><body><form>
          <label for="labeled">Preferred location <span>PRIVATE_LABEL_RENDERED_ANSWER</span></label>
          <input id="labeled" name="labeled">
          <label for="output-labeled">Work status <output>PRIVATE_LABEL_OUTPUT_ANSWER</output></label>
          <input id="output-labeled" name="output_labeled">
        </form></body></html>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                result = analyze_form_page(page)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["reason"], "UNSAFE_TEXT_CONTEXT")
                self.assertNotIn("PRIVATE_LABEL_RENDERED_ANSWER", json.dumps(result))
                self.assertNotIn("PRIVATE_LABEL_OUTPUT_ANSWER", json.dumps(result))
            finally:
                browser.close()

    def test_read_only_form_analysis_fails_closed_on_plain_rendered_label_heading_and_legend_text(self):
        from playwright.sync_api import sync_playwright

        cases = (
            (
                """<form><label for="field">PRIVATE_DIRECT_LABEL_ANSWER</label>
                   <input id="field" name="field"></form>""",
                ("PRIVATE_DIRECT_LABEL_ANSWER",),
            ),
            (
                """<form><section><h2>PRIVATE_DIRECT_HEADING_ANSWER</h2>
                   <fieldset><legend>PRIVATE_DIRECT_LEGEND_ANSWER</legend>
                   <input id="field" name="field"></fieldset></section></form>""",
                ("PRIVATE_DIRECT_HEADING_ANSWER", "PRIVATE_DIRECT_LEGEND_ANSWER"),
            ),
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for html, markers in cases:
                    with self.subTest(markers=markers):
                        page = browser.new_page()
                        page.set_content(f"<!doctype html><html><body>{html}</body></html>")
                        result = analyze_form_page(page)
                        self.assertEqual(result["status"], "UNKNOWN")
                        self.assertEqual(result["reason"], "UNSAFE_TEXT_CONTEXT")
                        serialized = json.dumps(result)
                        for marker in markers:
                            self.assertNotIn(marker, serialized)
                        page.close()
            finally:
                browser.close()

    def test_unknown_optional_form_analysis_blocks_before_browser_start(self):
        from unittest.mock import patch

        with fixture_server() as origin:
            app_id = self.prepared(origin)
            application = self.store.application(app_id)
            with self.store.engine.connect() as connection:
                route = dict(connection.execute(select(m.routes).where(
                    m.routes.c.id == application["route_id"])).mappings().one())
            profile = self.store.profile(application["profile_id"], application["profile_version"])
            original_get = MockClient.get
            legal_preflight_results = []

            def malformed_optional_control(client, url):
                response = original_get(client, url)
                if "/ats/questions/" not in url:
                    return response
                questionnaire = response.json()
                optional = next(row for row in questionnaire["questions"]
                                if row["id"] == "optional_note")
                optional["options"] = {"malformed": True}
                legal_preflight_results.append(build_preflight_result(
                    profile, route, questionnaire, {"resume", "cover-letter"},
                    application_id=app_id)["result"])
                return httpx.Response(response.status_code, json=questionnaire,
                                      request=response.request)

            with patch.object(MockClient, "get", malformed_optional_control):
                with patch("services.applications.mock_adapter.sync_playwright") as browser_start:
                    result = apply_mock(self.store, app_id, origin, self.root)
                    browser_start.assert_not_called()

            self.assertEqual(legal_preflight_results, ["PASS"])
            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            with self.store.engine.connect() as connection:
                attempts = connection.execute(select(m.attempts.c.id).where(
                    m.attempts.c.application_id == app_id)).all()
            self.assertEqual(attempts, [])
            self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(),
                             {"status": "not_found"})

    def test_dry_run_fills_two_pages_uploads_and_stops_before_submit(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            result = apply_mock(self.store, app_id, origin, self.root)
            self.assertEqual(result["state"], "DRY_RUN_COMPLETE")
            self.assertIn("UPLOAD_RESUME", result["trace"])
            self.assertIn("VALIDATE", result["trace"])
            self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(), {"status": "not_found"})

    def test_opt_in_browser_failure_capture_is_redacted_and_does_not_change_review_state(self):
        from playwright.sync_api import Locator
        from unittest.mock import patch

        with fixture_server() as origin:
            app_id = self.prepared(origin)
            original_fill = Locator.fill

            def fail_after_filling_email(locator, value, *args, **kwargs):
                result = original_fill(locator, value, *args, **kwargs)
                if locator.get_attribute("name") == "email":
                    raise RuntimeError("PRIVATE_FAILURE user@example.test token=secret-value")
                return result

            with patch.object(Locator, "fill", fail_after_filling_email):
                with self.assertRaisesRegex(RuntimeError, "PRIVATE_FAILURE"):
                    apply_mock(self.store, app_id, origin, self.root, debug_capture=True)

            self.assertEqual(self.store.application(app_id)["state"], "REVIEW_REQUIRED")
            captures = list((self.root / app_id / "debug-captures").glob("*/metadata.json"))
            self.assertEqual(len(captures), 1)
            metadata_path = captures[0]
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            dom_path = metadata_path.with_name("dom.json")
            self.assertEqual(metadata["current_step"], "FILL_CONTACT")
            self.assertEqual(metadata["error"]["type"], "RuntimeError")
            self.assertTrue((metadata_path.parent / "screenshot.png").is_file())
            serialized = metadata_path.read_text(encoding="utf-8") + dom_path.read_text(encoding="utf-8")
            self.assertNotIn("user@example.test", serialized)
            self.assertNotIn("secret-value", serialized)
            self.assertNotIn("PRIVATE_FAILURE", serialized)
            self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(), {"status": "not_found"})

    def test_opt_in_capture_preserves_unconfirmed_submission_without_retry(self):
        from unittest.mock import patch

        with fixture_server() as origin:
            app_id = self.prepared(origin)
            original_get = MockClient.get

            def hide_receipt(client, url):
                if "/ats/receipts/" in url:
                    return httpx.Response(404, json={"status": "not_found"})
                return original_get(client, url)

            with patch.object(MockClient, "get", hide_receipt):
                with self.assertRaisesRegex(ValueError, "Mock receipt missing"):
                    apply_mock(self.store, app_id, origin, self.root, submit=True, debug_capture=True)
                first = self.store.application(app_id)
                self.assertEqual(first["state"], "SUBMISSION_UNCONFIRMED")
                second = apply_mock(self.store, app_id, origin, self.root, submit=True, debug_capture=True)
                self.assertEqual(second["state"], "SUBMISSION_UNCONFIRMED")

            history = self.history()
            lifecycle = [event["type"] for event in history
                         if event["type"] in {"APPLICATION_SUBMITTED", "APPLICATION_CONFIRMED"}]
            self.assertEqual(lifecycle.count("APPLICATION_SUBMITTED"), 1)
            self.assertNotIn("APPLICATION_CONFIRMED", lifecycle)

            receipt = httpx.get(origin + "/ats/receipts/" + app_id).json()
            self.assertEqual(receipt["submission_count"], 1)
            captures = list((self.root / app_id / "debug-captures").glob("*/metadata.json"))
            self.assertEqual(len(captures), 1)

    def test_mock_submission_confirmed_once_and_survives_restart(self):
        with fixture_server() as origin:
            self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                     "email": "other@example.invalid"})
            app_id = self.prepared(origin)
            result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "CONFIRMED")
            self.assertIn("VERIFY_CONFIRMATION", result["trace"])
            confirmation = result["confirmation"]
            self.assertEqual(confirmation["confirmation_url"], origin + "/ats/submit/B")
            self.assertIn("Thank you for applying", confirmation["confirmation_text"])
            self.assertIn("Application received", confirmation["confirmation_text"])
            self.assertEqual(confirmation["reference_number"], confirmation["receipt"])
            self.assertIn(confirmation["reference_number"], confirmation["confirmation_text"])
            captured_at = datetime.fromisoformat(confirmation["timestamp"])
            self.assertIsNotNone(captured_at.tzinfo)
            reopened = Store(self.store.url)
            try:
                again = apply_mock(reopened, app_id, origin, self.root, submit=True)
                self.assertEqual(again["state"], "CONFIRMED")
                self.assertEqual(again["confirmation"], confirmation)
                self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json()["submission_count"], 1)
                history = self.history(store=reopened)
                lifecycle = [event for event in history
                             if event["type"] in {"APPLICATION_SUBMITTED", "APPLICATION_CONFIRMED"}]
                self.assertEqual([event["type"] for event in lifecycle],
                                 ["APPLICATION_CONFIRMED", "APPLICATION_SUBMITTED"])
                self.assertEqual(lifecycle[0]["summary"], "Fictional mock application outcome confirmed")
                self.assertTrue(all(event["subject"] and event["company"] for event in lifecycle))
                self.assertFalse(any(event["type"] in {"APPLICATION_SUBMITTED", "APPLICATION_CONFIRMED"}
                                     for event in self.history("other", reopened)))
            finally:
                reopened.engine.dispose()

    def test_unrecognized_confirmation_text_is_not_persisted_as_confirmed(self):
        from unittest.mock import patch

        with fixture_server() as origin:
            app_id = self.prepared(origin)
            original_get = MockClient.get

            def alter_confirmation(client, url):
                response = original_get(client, url)
                if "/ats/receipts/" in url:
                    payload = response.json()
                    payload["confirmation_text"] = "Your application is still being processed."
                    return httpx.Response(response.status_code, json=payload, request=response.request)
                return response

            with patch.object(MockClient, "get", alter_confirmation):
                with self.assertRaisesRegex(ValueError, "Mock receipt missing"):
                    apply_mock(self.store, app_id, origin, self.root, submit=True)
            application = self.store.application(app_id)
            self.assertEqual(application["state"], "SUBMISSION_UNCONFIRMED")
            self.assertIsNone(application["confirmation"])

    def test_timeout_after_submission_reservation_prevents_submit_click(self):
        from workers.operations import OperationTimedOut
        with fixture_server() as origin:
            app_id = self.prepared(origin)

            def expire_at_reserved_submit_boundary():
                if self.store.application(app_id)['state'] == 'SUBMITTING':
                    raise OperationTimedOut('OPERATION_TIMEOUT')

            with self.assertRaises(OperationTimedOut):
                apply_mock(self.store, app_id, origin, self.root, submit=True,
                           check=expire_at_reserved_submit_boundary)

            result = self.store.application(app_id)
            self.assertEqual(result['state'], 'SUBMISSION_UNCONFIRMED')
            self.assertEqual(httpx.get(origin + '/ats/receipts/' + app_id).json(),
                             {'status': 'not_found'})
            self.assertFalse(any(event["type"] in {"APPLICATION_SUBMITTED", "APPLICATION_CONFIRMED"}
                                 for event in self.history()))

    def test_playwright_actions_never_exceed_remaining_worker_budget(self):
        from playwright.sync_api import BrowserType, Page
        from unittest.mock import patch

        with fixture_server() as origin:
            app_id = self.prepared(origin)
            check = lambda: None
            check.remaining_seconds = lambda: 0.25
            action_timeouts = []
            navigation_timeouts = []
            launch_timeouts = []
            original_set_default_timeout = Page.set_default_timeout
            original_goto = Page.goto
            original_launch = BrowserType.launch_persistent_context

            def record_action_timeout(page, timeout):
                action_timeouts.append(timeout)
                return original_set_default_timeout(page, timeout)

            def record_navigation_timeout(page, url, *args, **kwargs):
                navigation_timeouts.append(kwargs.get('timeout'))
                return original_goto(page, url, *args, **kwargs)

            def record_launch_timeout(browser_type, *args, **kwargs):
                launch_timeouts.append(kwargs.get('timeout'))
                return original_launch(browser_type, *args, **kwargs)

            with patch.object(Page, 'set_default_timeout', record_action_timeout), \
                    patch.object(Page, 'goto', record_navigation_timeout), \
                    patch.object(BrowserType, 'launch_persistent_context', record_launch_timeout):
                result = apply_mock(self.store, app_id, origin, self.root, submit=True, check=check)

            self.assertEqual(result['state'], 'CONFIRMED')
            self.assertTrue(action_timeouts)
            self.assertTrue(all(0 < timeout <= 250 for timeout in action_timeouts))
            self.assertTrue(navigation_timeouts)
            self.assertTrue(all(0 < timeout <= 250 for timeout in navigation_timeouts))
            self.assertTrue(launch_timeouts)
            self.assertTrue(all(0 < timeout <= 250 for timeout in launch_timeouts))
            self.assertEqual(httpx.get(origin + '/ats/receipts/' + app_id).json()['submission_count'], 1)

    def test_preflight_http_requests_use_the_worker_operation_check(self):
        from unittest.mock import patch

        with fixture_server() as origin:
            app_id = self.prepared(origin)
            check_calls = 0

            def check():
                nonlocal check_calls
                check_calls += 1

            check.remaining_seconds = lambda: 30
            request_check_counts = []
            original_get = MockClient.get

            def record_request_checks(client, url):
                before = check_calls
                response = original_get(client, url)
                if "/ats/questions/" in url:
                    request_check_counts.append(check_calls - before)
                return response

            with patch.object(MockClient, "get", record_request_checks):
                result = apply_mock(self.store, app_id, origin, self.root, check=check)

            self.assertEqual(result["state"], "DRY_RUN_COMPLETE")
            self.assertEqual(len(request_check_counts), 2)
            self.assertTrue(all(count >= 1 for count in request_check_counts), request_check_counts)

    def test_uncertain_submission_without_receipt_does_not_retry(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            self.store.transition(app_id, ["PREPARED"], "SUBMITTING")
            result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "SUBMISSION_UNCONFIRMED")
            result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "SUBMISSION_UNCONFIRMED")
            self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(), {"status": "not_found"})
            self.assertFalse(any(event["type"] in {"APPLICATION_SUBMITTED", "APPLICATION_CONFIRMED"}
                                 for event in self.history()))

    def test_captcha_requires_intervention(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.routes).where(m.routes.c.id == self.store.application(app_id)["route_id"]).values(url=origin + "/ats/apply/B?challenge=true"))
            result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "USER_INTERVENTION_REQUIRED")

    def test_nonlocal_mock_origin_is_rejected_before_any_request(self):
        with self.assertRaises(ValueError):
            MockClient("https://example.com")
        with self.assertRaises(ValueError):
            MockClient("http://mock")

    def test_receipt_network_failure_is_persisted_as_uncertain(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            self.store.transition(app_id, ["PREPARED"], "SUBMITTING")
            from unittest.mock import patch
            with patch.object(MockClient, "get", side_effect=httpx.ConnectError("fixture receipt unavailable")):
                result = apply_mock(self.store, app_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "SUBMISSION_UNCONFIRMED")

    def test_hourly_limit_counts_submission_time_for_old_prepared_application(self):
        from services.applications.mock_adapter import _authorize
        from packages.domain.safety import Policy
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.applications).where(m.applications.c.id == app_id).values(created_at="2000-01-01T00:00:00+00:00"))
            self.assertEqual(apply_mock(self.store, app_id, origin, self.root, submit=True)["state"], "CONFIRMED")
            other_job = next(row for row in self.store.jobs() if row["requisition"] == "A")
            other = prepare(self.store, other_job["id"], "fixture", self.root)
            self.store.transition(other, ["PREPARED"], "READY_TO_SUBMIT")
            decision = _authorize(self.store, other, 100, Policy(hourly_limit=1))
            self.assertFalse(decision.allowed)
            self.assertIn("HOURLY_LIMIT_REACHED", decision.reasons)

    def test_changed_profile_does_not_change_prepared_contact_or_answers(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            old_version = self.store.application(app_id)["profile_version"]
            self.store.save_profile(dict(PROFILE, name="Changed Fixture", email="changed@example.test", answers=[]))
            self.assertEqual(self.store.profile("fixture", old_version)["name"], "Alex Fixture")
            result = apply_mock(self.store, app_id, origin, self.root)
            self.assertEqual(result["state"], "DRY_RUN_COMPLETE")

    def test_missing_required_answer_stops_before_browser_and_shows_question(self):
        from unittest.mock import patch
        profile = dict(PROFILE, answers=[row for row in PROFILE["answers"] if row["id"] != "remote"])
        with fixture_server() as origin:
            app_id = self.prepared(origin, profile)
            with patch("services.applications.mock_adapter.sync_playwright") as browser:
                result = apply_mock(self.store, app_id, origin, self.root)
            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertFalse(browser.called)
            from services.applications.preflight import latest_preflight
            report = latest_preflight(self.store, app_id)
            self.assertEqual(report["result"], "FAIL")
            self.assertEqual(report["missing"][0]["question_id"], "remote")
            self.assertEqual(report["missing"][0]["answer_ids"], ["remote"])

    def test_sensitive_required_question_persists_review_and_stops_before_browser(self):
        from unittest.mock import patch
        from services.applications.preflight import latest_preflight

        answer_value = "SYNTHETIC_SENSITIVE_CANDIDATE_ANSWER"
        profile = dict(PROFILE, answers=[*PROFILE["answers"], {
            "id": "gender-answer", "question": "What is your gender?",
            "text": answer_value, "source": "Synthetic test", "verified": True,
            "user_approved": True, "confidence": 100,
        }])
        with fixture_server() as origin:
            app_id = self.prepared(origin, profile)
            original_get = MockClient.get
            requested_urls = []

            def add_sensitive_question(client, url):
                requested_urls.append(url)
                response = original_get(client, url)
                if "/ats/questions/" not in url:
                    return response
                questionnaire = response.json()
                questionnaire["questions"].append({
                    "id": "gender", "label": "What is your gender?", "required": True,
                    "requirements": {"answer_ids": ["gender-answer"]},
                })
                return httpx.Response(response.status_code, json=questionnaire,
                                      request=response.request)

            with patch.object(MockClient, "get", add_sensitive_question), \
                    patch("services.applications.mock_adapter.sync_playwright") as browser:
                result = apply_mock(self.store, app_id, origin, self.root, submit=True)

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            browser.assert_not_called()
            self.assertTrue(requested_urls)
            self.assertTrue(all(urlsplit(url).scheme + "://" + urlsplit(url).netloc == origin
                                for url in requested_urls), requested_urls)
            with self.store.engine.connect() as connection:
                attempts = connection.execute(select(func.count()).select_from(m.attempts).where(
                    m.attempts.c.application_id == app_id)).scalar_one()
            self.assertEqual(attempts, 0)
            self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(),
                             {"status": "not_found"})
            report = latest_preflight(self.store, app_id)
            self.assertEqual(report["result"], "UNKNOWN")
            self.assertEqual(report["reason"], "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION")
            self.assertEqual(report["missing"], [{
                "question_id": "gender", "question": "What is your gender?",
            }])
            self.assertNotIn(answer_value, json.dumps(report))

            with TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                       operation_mode="FIXTURE")) as client:
                detail = client.get(f"/profiles/fixture/applications/{app_id}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["question_preflight"]["reason"],
                             "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION")
            self.assertEqual(detail.json()["question_preflight"]["missing"][0]["question"],
                             "What is your gender?")

    def test_conditional_required_dropdown_uses_known_preflight_mapping(self):
        profile = dict(PROFILE, answers=[*PROFILE["answers"], {
            "id": "state", "question": "State or province", "text": "Oklahoma",
            "source": "Synthetic test", "verified": True, "user_approved": True,
            "confidence": 100,
        }])
        with fixture_server() as origin:
            app_id = self.prepared(origin, profile)
            application = self.store.application(app_id)
            with self.store.engine.begin() as connection:
                route = connection.execute(select(m.routes).where(
                    m.routes.c.id == application["route_id"])).mappings().one()
                connection.execute(update(m.routes).where(m.routes.c.id == route["id"]).values(
                    url=origin + "/ats/apply/B?dynamic=known"))

            result = apply_mock(self.store, app_id, origin, self.root)

            self.assertEqual(result["state"], "DRY_RUN_COMPLETE")
            self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(),
                             {"status": "not_found"})
            self.assertEqual(httpx.get(origin + "/_test/request-counts").json(),
                             {"review": 1, "submit": 0})

    def test_unknown_conditional_required_field_is_reviewed_before_review_or_submit(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            application = self.store.application(app_id)
            with self.store.engine.begin() as connection:
                route = connection.execute(select(m.routes).where(
                    m.routes.c.id == application["route_id"])).mappings().one()
                connection.execute(update(m.routes).where(m.routes.c.id == route["id"]).values(
                    url=origin + "/ats/apply/B?dynamic=unknown"))

            def check():
                return None
            check.remaining_seconds = lambda: 0.5
            result = apply_mock(self.store, app_id, origin, self.root, submit=True, check=check)

            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertIn("UNKNOWN_DYNAMIC_REQUIRED_FIELD", result["trace"])
            from services.applications.preflight import latest_preflight
            preflight = latest_preflight(self.store, app_id)
            self.assertEqual(preflight["result"], "UNKNOWN")
            self.assertEqual(preflight["reason"], "UNKNOWN_DYNAMIC_REQUIRED_FIELD")
            self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(),
                             {"status": "not_found"})
            self.assertEqual(httpx.get(origin + "/_test/request-counts").json(),
                             {"review": 0, "submit": 0})

    def test_changed_questionnaire_stops_before_browser_as_stale(self):
        from unittest.mock import patch
        with fixture_server() as origin:
            app_id = self.prepared(origin)
            initial = httpx.get(origin + "/ats/questions/B").json()
            changed = dict(initial, revision=2)
            from services.applications.preflight import latest_preflight
            with patch.object(MockClient, "get", side_effect=[httpx.Response(200, json=initial),
                                                                httpx.Response(200, json=changed)]), \
                    patch("services.applications.mock_adapter.sync_playwright") as browser:
                result = apply_mock(self.store, app_id, origin, self.root)
            self.assertEqual(result["state"], "REVIEW_REQUIRED")
            self.assertFalse(browser.called)
            report = latest_preflight(self.store, app_id)
            self.assertEqual(report["result"], "UNKNOWN")
            self.assertEqual(report["reason"], "STALE_PREFLIGHT")


if __name__ == "__main__":
    unittest.main()
