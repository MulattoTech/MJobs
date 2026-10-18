"""Question preflight fails closed using structured local ATS metadata."""
from pathlib import Path
import tempfile
import unittest

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.preflight import (
    build_preflight_result,
    latest_preflight,
    persist_preflight,
    questionnaire_fingerprint,
)
from services.discovery.pipeline import MockClient, discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_mock_ats_exposes_bounded_read_only_question_metadata(self):
        with fixture_server() as origin:
            client = MockClient(origin)
            try:
                response = client.get(origin + "/ats/questions/B")
            finally:
                client.close()

        metadata = response.json()
        self.assertEqual(metadata["provider"], "mock-ats")
        self.assertEqual(metadata["tenant"], "example")
        self.assertEqual(metadata["requisition"], "B")
        self.assertGreater(metadata["revision"], 0)
        self.assertIn("fixture_question", {row["id"] for row in metadata["questions"]})
        app_id_question = next(row for row in metadata["questions"] if row["id"] == "application-id")
        self.assertEqual(app_id_question["requirements"], {"application_context": ["application_id"]})

    def test_missing_required_answer_reports_exact_question_and_mapping_without_values(self):
        profile = dict(PROFILE, answers=[row for row in PROFILE["answers"] if row["id"] != "remote"])
        questionnaire = {
            "provider": "mock-ats", "tenant": "example", "requisition": "B", "revision": 1,
            "questions": [{"id": "remote", "label": "Can you work remotely?", "required": True,
                           "requirements": {"answer_ids": ["remote"]}}],
        }

        result = build_preflight_result(profile, {"provider": "mock-ats", "tenant": "example",
                                                  "requisition": "B"}, questionnaire, {"resume", "cover-letter"})

        self.assertEqual(result["result"], "FAIL")
        self.assertEqual(result["missing"], [{
            "question_id": "remote", "question": "Can you work remotely?",
            "fact_ids": [], "answer_ids": ["remote"], "profile_paths": [],
            "document_kinds": [], "application_context": [],
        }])
        self.assertNotIn("Controlled fictional", str(result))

    def test_missing_optional_answer_does_not_fail_required_question_preflight(self):
        questionnaire = {
            "provider": "mock-ats", "tenant": "example", "requisition": "B", "revision": 1,
            "questions": [
                {"id": "country", "label": "Country", "required": True,
                 "requirements": {"answer_ids": ["country"]}},
                {"id": "optional", "label": "Optional note", "required": False,
                 "requirements": {"answer_ids": ["not-saved"]}},
            ],
        }

        result = build_preflight_result(PROFILE, {"provider": "mock-ats", "tenant": "example",
                                                   "requisition": "B"}, questionnaire, set())

        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["missing"], [])

    def test_missing_required_fact_is_reported_by_identifier(self):
        questionnaire = {
            "provider": "mock-ats", "tenant": "example", "requisition": "B", "revision": 1,
            "questions": [{"id": "python-years", "label": "Python experience", "required": True,
                           "requirements": {"fact_ids": ["python-years"]}}],
        }
        result = build_preflight_result(PROFILE, {"provider": "mock-ats", "tenant": "example",
                                                   "requisition": "B"}, questionnaire, set())
        self.assertEqual(result["result"], "FAIL")
        self.assertEqual(result["missing"][0]["fact_ids"], ["python-years"])
        self.assertNotIn("Alex Fixture", str(result))

    def test_unsupported_ats_is_unknown_and_never_claims_a_pass(self):
        result = build_preflight_result(PROFILE, {"provider": "workday", "tenant": "tenant"}, None, set())
        self.assertEqual(result["result"], "UNKNOWN")
        self.assertEqual(result["reason"], "UNSUPPORTED_ATS")

    def test_unmapped_required_question_is_unknown_and_stale_fingerprint_is_detected(self):
        route = {"provider": "mock-ats", "tenant": "example", "requisition": "B"}
        first = {"provider": "mock-ats", "tenant": "example", "requisition": "B", "revision": 1,
                 "questions": [{"id": "new", "label": "New required question", "required": True,
                                "requirements": {}}]}
        changed = dict(first, revision=2)

        result = build_preflight_result(PROFILE, route, first, set())

        self.assertEqual(result["result"], "UNKNOWN")
        self.assertEqual(result["reason"], "UNMAPPED_REQUIRED_QUESTION")
        self.assertNotEqual(questionnaire_fingerprint(first), questionnaire_fingerprint(changed))

    def test_required_sensitive_question_is_unknown_even_with_valid_mapping_and_no_answer_values(self):
        sensitive_answer = "SYNTHETIC_CANDIDATE_ANSWER_MUST_NOT_BE_AUDITED"
        profile = dict(PROFILE, answers=[*PROFILE["answers"], {
            "id": "gender-answer", "question": "What is your gender?",
            "text": sensitive_answer, "source": "Synthetic test", "verified": True,
            "user_approved": True, "confidence": 100,
        }])
        questionnaire = {
            "provider": "mock-ats", "tenant": "example", "requisition": "B", "revision": 1,
            "questions": [
                {"id": "country", "label": "Country", "required": True,
                 "requirements": {"answer_ids": ["country"]}},
                {"id": "gender", "label": "What is your gender?", "required": True,
                 "requirements": {"answer_ids": ["gender-answer"]}},
            ],
        }

        result = build_preflight_result(profile, {"provider": "mock-ats", "tenant": "example",
                                                  "requisition": "B"}, questionnaire,
                                       {"resume", "cover-letter"})

        self.assertEqual(result["result"], "UNKNOWN")
        self.assertEqual(result["reason"], "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION")
        self.assertEqual(result["missing"], [{
            "question_id": "gender", "question": "What is your gender?",
        }])
        self.assertNotIn(sensitive_answer, str(result))

        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs() if row["requisition"] == "B")
            application_id = prepare(self.store, job["id"], "fixture", self.root)
            persisted = dict(result, document_version=self.store.application(application_id)["document_version"])
            persist_preflight(self.store, application_id, persisted)

        stored = latest_preflight(self.store, application_id)
        with self.store.engine.connect() as connection:
            event = connection.execute(m.audit.select().where(
                m.audit.c.entity_id == application_id,
                m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT")).mappings().one()
        self.assertEqual(stored["result"], "UNKNOWN")
        self.assertFalse(stored["is_current"])
        self.assertEqual(stored["reason"], "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION")
        self.assertEqual(stored["missing"][0]["question_id"], "gender")
        self.assertNotIn(sensitive_answer, str(stored))
        self.assertNotIn(sensitive_answer, str(event["payload"]))

    def test_sensitive_identity_questions_are_classified_without_answer_evaluation(self):
        route = {"provider": "mock-ats", "tenant": "example", "requisition": "B"}
        for question_id, label in (
            ("ssn", "What is your Social Security number?"),
            ("birth-date", "What is your date of birth?"),
        ):
            with self.subTest(label=label):
                questionnaire = {
                    "provider": "mock-ats", "tenant": "example", "requisition": "B", "revision": 1,
                    "questions": [{"id": question_id, "label": label, "required": True,
                                   "requirements": {"answer_ids": ["country"]}}],
                }
                result = build_preflight_result(PROFILE, route, questionnaire,
                                                {"resume", "cover-letter"})
                self.assertEqual(result["result"], "UNKNOWN")
                self.assertEqual(result["reason"], "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION")

    def test_legal_work_and_background_screening_paraphrases_fail_closed(self):
        route = {"provider": "mock-ats", "tenant": "example", "requisition": "B"}
        for question_id, label in (
            ("legal-permission", "Do you have legal permission to work in the United States?"),
            ("permitted-work", "Are you permitted to work in the United States?"),
            ("background-screen", "Have you undergone a background screening?"),
        ):
            with self.subTest(label=label):
                questionnaire = {
                    "provider": "mock-ats", "tenant": "example", "requisition": "B", "revision": 1,
                    "questions": [{"id": question_id, "label": label, "required": True,
                                   "requirements": {"answer_ids": ["country"]}}],
                }
                result = build_preflight_result(PROFILE, route, questionnaire,
                                                {"resume", "cover-letter"})
                self.assertEqual(result["result"], "UNKNOWN")
                self.assertEqual(result["reason"], "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION")

    def test_preflight_is_persisted_and_readable_without_answer_values(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs() if row["requisition"] == "B")
            application_id = prepare(self.store, job["id"], "fixture", self.root)
            application = self.store.application(application_id)
            result = {"result": "FAIL", "reason": None, "profile_version": application["profile_version"],
                      "document_version": application["document_version"], "metadata_fingerprint": "a" * 64,
                      "missing": [{"question_id": "remote", "question": "Remote?", "answer_ids": ["remote"]}],
                      "observed_at": "2026-10-06T00:00:00Z"}
            persist_preflight(self.store, application_id, result)

        stored = latest_preflight(self.store, application_id)
        self.assertEqual(stored["result"], "FAIL")
        self.assertFalse(stored["is_current"])
        self.assertEqual(stored["missing"][0]["answer_ids"], ["remote"])
        self.assertNotIn("Controlled fictional", str(stored))
        with self.store.engine.connect() as connection:
            event = connection.execute(m.audit.select().where(
                m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT")).mappings().one()
        self.assertEqual(event["payload"]["missing"], stored["missing"])
