"""Legally significant profile values remain explicit and fail closed."""
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications import mock_adapter
from services.applications.preflight import (
    build_preflight_result,
    latest_preflight,
    persist_preflight,
)
from services.discovery.pipeline import MockClient, discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROUTE = {"provider": "mock-ats", "tenant": "example", "requisition": "B"}


def question(label, path, question_id="legal-question"):
    return {
        "id": question_id,
        "label": label,
        "required": True,
        "requirements": {"profile_paths": [path]},
    }


def questionnaire(*questions):
    return {
        "provider": "mock-ats", "tenant": "example", "requisition": "B",
        "revision": 1, "questions": list(questions),
    }


class LegalQuestionPreflightTests(unittest.TestCase):
    def profile(self, work_authorization="Authorized to work in the United States",
                sponsorship_needs="NOT_REQUIRED"):
        return {
            **PROFILE,
            "preferences": {
                "work_authorization": work_authorization,
                "sponsorship_needs": sponsorship_needs,
            },
        }

    def evaluate(self, profile, item):
        return build_preflight_result(profile, ROUTE, questionnaire(item), {"resume", "cover-letter"})

    def test_explicit_work_authorization_and_sponsorship_mappings_are_supported_without_echo(self):
        authorization = "Authorized to work in the United States"
        sponsorship = "NOT_REQUIRED"
        profile = self.profile(authorization, sponsorship)

        authorization_result = self.evaluate(
            profile, question("Are you legally authorized for employment in the United States?",
                              "preferences.work_authorization", "work-authorization"))
        sponsorship_result = self.evaluate(
            profile, question("Will you require sponsorship?",
                              "preferences.sponsorship_needs", "sponsorship"))

        self.assertEqual(authorization_result["result"], "PASS")
        self.assertEqual(sponsorship_result["result"], "PASS")
        self.assertEqual(authorization_result["mappings"][0]["profile_paths"],
                         ["preferences.work_authorization"])
        self.assertEqual(sponsorship_result["mappings"][0]["profile_paths"],
                         ["preferences.sponsorship_needs"])
        self.assertNotIn(authorization, str(authorization_result))
        self.assertNotIn(sponsorship, str(sponsorship_result))

    def test_missing_or_ambiguous_values_remain_unknown(self):
        item = question("Are you authorized to work in the United States?",
                        "preferences.work_authorization")
        for value in ("", "not sure", "unknown", "I am not sure whether I am authorized"):
            with self.subTest(value=value):
                result = self.evaluate(self.profile(work_authorization=value), item)
                self.assertEqual(result["result"], "UNKNOWN")
                self.assertEqual(result["reason"], "LEGAL_PROFILE_VALUE_UNAVAILABLE")
                self.assertEqual(result["missing"], [{
                    "question_id": "legal-question",
                    "question": "Are you authorized to work in the United States?",
                }])

        result = self.evaluate(self.profile(sponsorship_needs="NOT_SPECIFIED"), question(
            "Will you require sponsorship?", "preferences.sponsorship_needs"))
        self.assertEqual(result["result"], "UNKNOWN")
        self.assertEqual(result["reason"], "LEGAL_PROFILE_VALUE_UNAVAILABLE")

    def test_unrelated_or_mismapped_legal_attestations_remain_unknown(self):
        profile = self.profile()
        cases = (
            question("Have you ever been convicted of a felony?",
                     "preferences.work_authorization", "criminal-history"),
            question("Are you authorized to work in the United States?",
                     "preferences.sponsorship_needs", "wrong-field"),
            {
                "id": "combined-fields", "label": "Are you authorized to work in the United States?",
                "required": True,
                "requirements": {"profile_paths": ["preferences.work_authorization",
                                                     "preferences.sponsorship_needs"]},
            },
            question("Are you authorized to work in the United States?", "work_authorization",
                     "unsupported-path"),
        )
        for item in cases:
            with self.subTest(question=item["id"]):
                result = self.evaluate(profile, item)
                self.assertEqual(result["result"], "UNKNOWN")
                self.assertIn(result["reason"], {
                    "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION", "UNMAPPED_REQUIRED_QUESTION",
                })

    def test_ordinary_question_mapping_still_works(self):
        result = self.evaluate(PROFILE, {
            "id": "country", "label": "Country of residence", "required": True,
            "requirements": {"answer_ids": ["country"]},
        })
        self.assertEqual(result["result"], "PASS")

    def test_preflight_persistence_redacts_profile_value_and_unknown_stops_before_browser(self):
        sensitive_value = "SYNTHETIC_LEGAL_PROFILE_VALUE_DO_NOT_PERSIST"
        with tempfile.TemporaryDirectory() as directory, fixture_server() as origin:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'test.db').as_posix()}")
            store.migrate()
            store.save_profile(self.profile(work_authorization=sensitive_value))
            discover(store, origin, "fixture")
            job = next(row for row in store.jobs() if row["requisition"] == "B")
            application_id = prepare(store, job["id"], "fixture", root)

            item = question("Have you ever been convicted of a felony?",
                            "preferences.work_authorization", "criminal-history")
            metadata = questionnaire(item)

            class Response:
                status_code = 200

                @staticmethod
                def json():
                    return metadata

            original_get = MockClient.get

            def get_with_legal_question(client, url):
                if "/ats/questions/" in url:
                    return Response()
                return original_get(client, url)

            try:
                with patch.object(MockClient, "get", get_with_legal_question), \
                     patch.object(mock_adapter, "sync_playwright") as browser:
                    application = mock_adapter.apply_mock(
                        store, application_id, origin, root, submit=False)
                self.assertEqual(application["state"], "REVIEW_REQUIRED")
                self.assertEqual(store.count("application_attempts"), 0)
                browser.assert_not_called()
                stored = latest_preflight(store, application_id)
                self.assertEqual(stored["result"], "UNKNOWN")
                self.assertNotIn(sensitive_value, str(stored))
                with store.engine.connect() as connection:
                    rows = connection.execute(
                        m.audit.select().where(
                            m.audit.c.entity_id == application_id,
                            m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT",
                        )).mappings().all()
                self.assertEqual(len(rows), 1)
                self.assertNotIn(sensitive_value, str(rows[0]["payload"]))
            finally:
                store.engine.dispose()

    def test_explicit_value_audit_round_trip_contains_mapping_but_not_value(self):
        sensitive_value = "SYNTHETIC_LEGAL_PROFILE_VALUE_DO_NOT_PERSIST"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(f"sqlite:///{(Path(directory) / 'test.db').as_posix()}")
            store.migrate()
            store.save_profile(self.profile(work_authorization=sensitive_value))
            result = self.evaluate(self.profile(work_authorization=sensitive_value), question(
                "Are you authorized to work in the United States?",
                "preferences.work_authorization"))
            persist_preflight(store, "synthetic-application", result)
            reopened = Store(store.url)
            try:
                stored = latest_preflight(reopened, "synthetic-application")
                self.assertEqual(stored["result"], "PASS")
                self.assertEqual(stored["mappings"][0]["profile_paths"],
                                 ["preferences.work_authorization"])
                self.assertNotIn(sensitive_value, str(stored))
                with reopened.engine.connect() as connection:
                    payload = connection.execute(
                        m.audit.select().where(
                            m.audit.c.entity_id == "synthetic-application",
                            m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT",
                        )).mappings().one()["payload"]
                self.assertNotIn(sensitive_value, str(payload))
            finally:
                reopened.engine.dispose()
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
