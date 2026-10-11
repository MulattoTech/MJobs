"""Controlled mock-ATS integration coverage for reviewed answer slot binding."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.applications.mock_adapter import apply_mock
from services.applications.preflight import latest_preflight
from services.discovery.pipeline import discover, MockClient
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class MockAnswerBindingFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-mock-answer-bindings-")
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

    @staticmethod
    def reviewed_answers():
        return [
            {"id": "ui-country-answer-1", "question": "Country", "text": "United States",
             "source": "User-reviewed fixture", "verified": True,
             "user_approved": True, "confidence": 95},
            {"id": "ui-remote-answer-2", "question": "Remote role", "text": "yes",
             "source": "User-reviewed fixture", "verified": True,
             "user_approved": True, "confidence": 95},
            {"id": "ui-fixture-answer-3", "question": "Approved fixture answer",
             "text": "Controlled fictional test application.",
             "source": "User-reviewed fixture", "verified": True,
             "user_approved": True, "confidence": 95},
        ]

    def assert_review_before_browser(self, origin, profile):
        app_id = self.prepared(origin, profile)
        with patch("services.applications.mock_adapter.sync_playwright") as browser:
            result = apply_mock(self.store, app_id, origin, self.root)
        self.assertEqual(result["state"], "REVIEW_REQUIRED")
        browser.assert_not_called()
        self.assertEqual(httpx.get(origin + "/ats/receipts/" + app_id).json(),
                         {"status": "not_found"})
        self.assertEqual(httpx.get(origin + "/_test/request-counts").json(),
                         {"review": 0, "submit": 0})
        report = latest_preflight(self.store, app_id)
        self.assertNotIn("answer text", json.dumps(report).casefold())
        return app_id, report

    def test_dry_run_binds_random_ids_by_exact_known_question_without_submission(self):
        profile = dict(PROFILE, answers=self.reviewed_answers())
        with fixture_server() as origin:
            app_id = self.prepared(origin, profile)
            result = apply_mock(self.store, app_id, origin, self.root)
            receipt = httpx.get(origin + "/ats/receipts/" + app_id).json()
            counts = httpx.get(origin + "/_test/request-counts").json()

        self.assertEqual(result["state"], "DRY_RUN_COMPLETE")
        self.assertIsNone(result["confirmation"])
        self.assertEqual(receipt, {"status": "not_found"})
        self.assertEqual(counts, {"review": 1, "submit": 0})
        self.assertEqual(self.store.profile("fixture")["answers"], profile["answers"])
        report = latest_preflight(self.store, app_id)
        mapped = {row["question_id"]: row["answer_ids"] for row in report["mappings"]}
        self.assertEqual(mapped["country"], ["ui-country-answer-1"])
        self.assertEqual(mapped["remote"], ["ui-remote-answer-2"])
        self.assertEqual(mapped["fixture_question"], ["ui-fixture-answer-3"])
        self.assertNotIn("Controlled fictional test application.", json.dumps(report))

    def test_ambiguous_duplicate_question_binding_stops_before_browser(self):
        answers = [*PROFILE["answers"],
                   {**self.reviewed_answers()[0],
                    "id": "ui-country-answer-duplicate", "text": "Canada"}]
        with fixture_server() as origin:
            _, report = self.assert_review_before_browser(origin, dict(PROFILE, answers=answers))
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["missing"][0]["question_id"], "country")
        self.assertNotIn("Canada", json.dumps(report))

    def test_unapproved_exact_question_binding_stops_before_browser(self):
        answers = [row for row in PROFILE["answers"] if row["id"] != "remote"]
        answers.append({**self.reviewed_answers()[1], "user_approved": False})
        with fixture_server() as origin:
            _, report = self.assert_review_before_browser(origin, dict(PROFILE, answers=answers))
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("remote", {row["question_id"] for row in report["missing"]})
        self.assertNotIn("yes", json.dumps(report))

    def test_edit_invalidated_question_answer_does_not_bind(self):
        edited = [
            {**PROFILE["answers"][0], "question": "Country",
             "text": "Changed after approval"},
            *PROFILE["answers"][1:],
        ]
        with fixture_server() as origin:
            app_id = self.prepared(origin, dict(PROFILE, answers=edited))
            saved = self.store.profile("fixture")
            self.assertFalse(saved["answers"][0]["verified"])
            result = apply_mock(self.store, app_id, origin, self.root)
            report = latest_preflight(self.store, app_id)
        self.assertEqual(result["state"], "REVIEW_REQUIRED")
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["missing"][0]["question_id"], "country")
        self.assertNotIn("Changed after approval", json.dumps(report))

    def test_wrong_question_on_legacy_slot_id_does_not_fall_back_to_id(self):
        wrong_question = [
            {**PROFILE["answers"][0], "question": "What is your preferred shift?"},
            *PROFILE["answers"][1:],
        ]
        with fixture_server() as origin:
            self.store.save_profile(dict(PROFILE, answers=wrong_question))
            reviewed = self.store.profile("fixture")
            reviewed["answers"][0]["verified"] = True
            reviewed["answers"][0]["user_approved"] = True
            self.store.save_profile(reviewed)
            _, report = self.assert_review_before_browser(origin, None)
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["missing"][0]["question_id"], "country")
        self.assertNotIn("United States", json.dumps(report))

    def test_optional_sensitive_question_never_binds_even_when_saved_answer_exists(self):
        sensitive_text = "SYNTHETIC_LEGAL_ANSWER_MUST_NOT_LEAK"
        profile = dict(PROFILE, answers=[*PROFILE["answers"], {
            "id": "ui-legal-answer", "question": "Are you eligible to work?",
            "text": sensitive_text, "source": "User-reviewed fixture",
            "verified": True, "user_approved": True, "confidence": 100,
        }])
        with fixture_server() as origin:
            app_id = self.prepared(origin, profile)
            original_get = MockClient.get

            def add_optional_sensitive_question(client, url):
                response = original_get(client, url)
                if "/ats/questions/" not in url:
                    return response
                questionnaire = response.json()
                questionnaire["questions"].append({
                    "id": "legal-optional", "label": "Are you eligible to work?",
                    "required": False,
                    "requirements": {"answer_ids": ["ui-legal-answer"]},
                })
                return httpx.Response(response.status_code, json=questionnaire,
                                      request=response.request)

            with patch.object(MockClient, "get", add_optional_sensitive_question), \
                    patch("services.applications.mock_adapter.sync_playwright") as browser:
                result = apply_mock(self.store, app_id, origin, self.root)
            receipt = httpx.get(origin + "/ats/receipts/" + app_id).json()
            counts = httpx.get(origin + "/_test/request-counts").json()
        self.assertEqual(result["state"], "REVIEW_REQUIRED")
        browser.assert_not_called()
        report = latest_preflight(self.store, app_id)
        self.assertEqual(report["result"], "UNKNOWN")
        self.assertEqual(report["reason"], "FORM_ANALYSIS_REQUIRES_REVIEW")
        self.assertNotIn(sensitive_text, json.dumps(report))
        self.assertEqual(receipt, {"status": "not_found"})
        self.assertEqual(counts, {"review": 0, "submit": 0})

    def test_malformed_known_slot_question_label_fails_closed_before_browser(self):
        with fixture_server() as origin:
            app_id = self.prepared(origin, dict(PROFILE, answers=self.reviewed_answers()))
            original_get = MockClient.get

            def remove_country_label(client, url):
                response = original_get(client, url)
                if "/ats/questions/" not in url:
                    return response
                questionnaire = response.json()
                next(question for question in questionnaire["questions"]
                     if question["id"] == "country")["label"] = None
                return httpx.Response(response.status_code, json=questionnaire,
                                      request=response.request)

            with patch.object(MockClient, "get", remove_country_label), \
                    patch("services.applications.mock_adapter.sync_playwright") as browser:
                result = apply_mock(self.store, app_id, origin, self.root)
            receipt = httpx.get(origin + "/ats/receipts/" + app_id).json()
            counts = httpx.get(origin + "/_test/request-counts").json()

        self.assertEqual(result["state"], "REVIEW_REQUIRED")
        browser.assert_not_called()
        report = latest_preflight(self.store, app_id)
        self.assertEqual(report["result"], "UNKNOWN")
        self.assertEqual(receipt, {"status": "not_found"})
        self.assertEqual(counts, {"review": 0, "submit": 0})


if __name__ == "__main__":
    unittest.main()
