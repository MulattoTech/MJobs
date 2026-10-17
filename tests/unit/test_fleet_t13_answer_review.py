"""Connected answer-review gate coverage for Team 13's public API surface."""
import unittest

from packages.persistence.store import Store
from services.answers.review import assess_answer_review
from services.documents.answers import (
    get_answer_confidence_policy,
    save_answer_confidence_policy,
)
from tests.integration import test_application_answer_matching_api as answer_fixture


class FleetT13AnswerReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = answer_fixture.ApplicationAnswerMatchingApiTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    @property
    def store(self):
        return self.fixture.store

    @property
    def client(self):
        return self.fixture.client

    def _url(self, *args, **kwargs):
        return self.fixture._url(*args, **kwargs)

    def _state_digest(self):
        return self.fixture._state_digest()

    def test_match_response_is_bound_to_persisted_profile_snapshot(self):
        save_answer_confidence_policy(self.store, "fixture", {
            "minimum_saved_answer_confidence": 80,
            "minimum_question_match_confidence": 1.0,
        })
        before = self._state_digest()

        response = self.client.post(self._url(), json={
            "question": "How many years of professional experience have you had?",
        })

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        review = body["review_gate"]
        self.assertFalse(review["review_required"])
        self.assertIsNone(review["reason"])
        self.assertEqual(len(review["fingerprint"]), 64)
        self.assertEqual([item["answer_id"] for item in body["candidates"]],
                         ["exact-experience"])

        reopened = Store(self.store.engine.url.render_as_string(hide_password=False))
        try:
            persisted_profile = reopened.profile("fixture")
            policy = get_answer_confidence_policy(reopened, "fixture")
            readback = assess_answer_review(
                body["question"], persisted_profile["answers"], policy,
            )
            self.assertEqual(readback["fingerprint"], review["fingerprint"])
            self.assertFalse(readback["review_required"])
        finally:
            reopened.engine.dispose()

        self.assertEqual(self._state_digest(), before)

    def test_unknown_conflicting_and_changed_snapshots_require_review(self):
        unknown = self.client.post(self._url(), json={
            "question": "What is your favorite fictional dessert?",
        })
        self.assertEqual(unknown.status_code, 200, unknown.text)
        self.assertEqual(unknown.json()["review_gate"]["reason"], "UNKNOWN_ANSWER")
        self.assertTrue(unknown.json()["review_gate"]["review_required"])
        self.assertEqual(unknown.json()["candidates"], [])

        profile = self.store.profile("fixture")
        profile["answers"].append({
            "id": "conflicting-experience",
            "question": "How many years of professional experience have you had?",
            "text": "A contradictory synthetic answer.",
            "source": "Synthetic user-authored example",
            "verified": True,
            "user_approved": True,
            "confidence": 96,
        })
        self.store.save_profile(profile)
        conflict = self.client.post(self._url(), json={
            "question": "How many years of professional experience have you had?",
        })
        self.assertEqual(conflict.status_code, 200, conflict.text)
        self.assertEqual(conflict.json()["review_gate"]["reason"],
                         "CONFLICTING_ANSWERS")
        self.assertTrue(conflict.json()["review_gate"]["review_required"])
        self.assertEqual(conflict.json()["candidates"], [])

        current = self.store.profile("fixture")
        previous = self.client.post(self._url(), json={
            "question": "Which ITSM tools have you used?",
        })
        self.assertEqual(previous.status_code, 200, previous.text)
        fingerprint = previous.json()["review_gate"]["fingerprint"]
        next(answer for answer in current["answers"]
             if answer["id"] == "semantic-itsm")["text"] = (
                 "Changed synthetic answer after the review pause."
             )
        self.store.save_profile(current)

        resumed = self.client.post(self._url(), json={
            "question": "Which ITSM tools have you used?",
            "expected_fingerprint": fingerprint,
        })
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["review_gate"]["reason"], "CHANGED_ANSWER")
        self.assertTrue(resumed.json()["review_gate"]["review_required"])
        self.assertEqual(resumed.json()["candidates"], [])

    def test_sensitive_question_returns_a_review_gate_without_saved_values(self):
        for question in (
            "What is your ethnicity?",
            "Are you legally authorized to work in the United States?",
            "What is your expected salary?",
            "Have you been arrested?",
            "Are you 18 years or older?",
            "Do you have a disability?",
        ):
            with self.subTest(question=question):
                response = self.client.post(self._url(), json={"question": question})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"]["review_gate"]["reason"],
                                 "SENSITIVE_QUESTION")
                self.assertTrue(
                    response.json()["detail"]["review_gate"]["review_required"]
                )
                self.assertNotIn("medical-question", response.text)

    def test_malformed_resume_fingerprint_cannot_restart_matching(self):
        before = self._state_digest()
        for fingerprint in (None, "A" * 64, "short", 123):
            with self.subTest(fingerprint=fingerprint):
                response = self.client.post(self._url(), json={
                    "question": "How many years of professional experience have you had?",
                    "expected_fingerprint": fingerprint,
                })
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"]["code"],
                                 "ANSWER_MATCH_INVALID_REVIEW_FINGERPRINT")
                self.assertNotIn("candidates", response.json()["detail"])
                self.assertNotIn("exact-experience", response.text)
        self.assertEqual(self._state_digest(), before)


if __name__ == "__main__":
    unittest.main()
