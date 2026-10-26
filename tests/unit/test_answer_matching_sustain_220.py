"""Sustained API-boundary coverage for MJ-REQ-219 answer matching."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.application_answer_matching import (
    create_application_answer_matching_router,
)
from services.documents.tailor import target_fingerprint


POLICY = {
    "minimum_saved_answer_confidence": 80,
    "minimum_question_match_confidence": 0.75,
}


class MemoryStore:
    """Small deterministic store implementing the production route's read API."""

    def __init__(self, answers):
        self.answers = deepcopy(answers)
        self.job_row = {
            "id": "job-synthetic",
            "title": "Synthetic role",
            "skills": [],
            "responsibilities": [],
            "description": "Synthetic fixture only",
        }
        self.application_row = {
            "id": "application-synthetic",
            "profile_id": "profile-synthetic",
            "job_id": "job-synthetic",
        }

    def profile(self, profile_id):
        if profile_id != "profile-synthetic":
            from sqlalchemy.exc import NoResultFound
            raise NoResultFound()
        return {"id": profile_id, "version": 1, "answers": deepcopy(self.answers)}

    def application(self, application_id):
        if application_id != self.application_row["id"]:
            from sqlalchemy.exc import NoResultFound
            raise NoResultFound()
        return dict(self.application_row)

    def job(self, job_id, profile_id):
        if job_id != self.job_row["id"] or profile_id != "profile-synthetic":
            from sqlalchemy.exc import NoResultFound
            raise NoResultFound()
        return dict(self.job_row)


def approved_answer(answer_id, question, text="Synthetic approved answer"):
    return {
        "id": answer_id,
        "question": question,
        "text": text,
        "source": "Synthetic test fixture",
        "verified": True,
        "user_approved": True,
        "confidence": 95,
    }


class AnswerMatchingApiSustain220Tests(unittest.TestCase):
    def setUp(self):
        self.answers = [
            approved_answer("exact", "How many years of experience?"),
            approved_answer("normalized", "HOW MANY YEARS OF EXPERIENCE!"),
            approved_answer("fuzzy", "How many years of professional experience do you have?"),
            approved_answer("semantic", "Which programming languages have you used?"),
            approved_answer("conflict", "How many years of professional experience have you had?", "A different synthetic experience answer"),
            approved_answer("sensitive", "Do you have a medical condition?", "synthetic only"),
        ]
        self.store = MemoryStore(self.answers)
        app = FastAPI()
        app.include_router(create_application_answer_matching_router(self.store))
        self.client = TestClient(app)
        self.url = (
            "/profiles/profile-synthetic/applications/application-synthetic/"
            "answers/match"
        )
        self.patches = (
            patch(
                "apps.api.application_answer_matching.answer_service."
                "get_answer_confidence_policy",
                return_value=POLICY,
            ),
            patch(
                "apps.api.application_answer_matching.assess_answer_review",
                side_effect=self._assess,
            ),
            patch(
                "apps.api.application_answer_matching.list_stories",
                return_value=[],
            ),
        )
        for mocked in self.patches:
            mocked.start()

    def tearDown(self):
        for mocked in reversed(self.patches):
            mocked.stop()
        self.client.close()

    def _assess(self, question, answers, policy, expected_fingerprint=None):
        from services.documents.answers import match_answer_question

        matches = []
        for answer in answers:
            match = match_answer_question(
                question,
                answer.get("question", ""),
                minimum_confidence=policy["minimum_question_match_confidence"],
            )
            if match:
                matches.append({"answer_id": answer["id"], **match})
        distinct = {answer["text"].strip() for answer in answers
                    if any(item["answer_id"] == answer["id"] for item in matches)}
        reason = "CONFLICTING_ANSWERS" if len(distinct) > 1 else None
        fingerprint = "a" * 64
        if expected_fingerprint is not None and expected_fingerprint != fingerprint:
            reason = "CHANGED_ANSWER"
        return {
            "matches": matches,
            "review_required": reason is not None,
            "reason": reason,
            "fingerprint": fingerprint,
        }

    def test_exact_normalized_fuzzy_and_semantic_methods_use_real_route(self):
        cases = (
            ("How many years of experience?", "EXACT_NORMALIZATION"),
            ("how many years of experience.", "EXACT_NORMALIZATION"),
            (
                "How many years of professional experience have you had?",
                "FUZZY",
            ),
            (
                "What coding languages have you worked with?",
                "CURATED_SEMANTIC",
            ),
        )
        for question, expected_method in cases:
            with self.subTest(method=expected_method, question=question):
                selected = {
                    "EXACT_NORMALIZATION": self.answers[0],
                    "FUZZY": self.answers[2],
                    "CURATED_SEMANTIC": self.answers[3],
                }[expected_method]
                self.store.answers = [deepcopy(selected)]
                response = self.client.post(self.url, json={"question": question})
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertFalse(body["review_gate"]["review_required"])
                self.assertTrue(body["candidates"])
                self.assertTrue(all(item["method"] == expected_method
                                    for item in body["candidates"]))
                self.assertTrue(all(item["verified"] and item["user_approved"]
                                    for item in body["candidates"]))

    def test_conflicting_answers_fail_closed_without_disclosing_either_value(self):
        self.store.answers = [
            approved_answer(
                "shared", "How many years of professional experience do you have?",
                "shared synthetic experience answer",
            ),
            approved_answer(
                "conflict", "How many years of professional experience have you had?",
                "different synthetic experience answer",
            ),
        ]
        response = self.client.post(self.url, json={
            "question": "How many years of professional experience have you had?",
        })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["review_gate"]["review_required"])
        self.assertEqual(body["review_gate"]["reason"], "CONFLICTING_ANSWERS")
        self.assertEqual(body["candidates"], [])
        self.assertNotIn("shared synthetic experience answer", response.text)
        self.assertNotIn("different synthetic experience answer", response.text)

    def test_sensitive_prompt_is_rejected_without_echoing_saved_sensitive_answer(self):
        response = self.client.post(self.url, json={
            "question": "Do you have a medical condition?",
        })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "ANSWER_MATCH_SENSITIVE_QUESTION",
        )
        self.assertNotIn("synthetic only", response.text)

    def test_invalid_payload_and_cross_profile_access_fail_closed(self):
        invalid = self.client.post(self.url, json={"question": " "})
        self.assertEqual(invalid.status_code, 422, invalid.text)
        cross_profile = self.client.post(
            self.url.replace("profile-synthetic", "other-profile"),
            json={"question": "How many years of experience?"},
        )
        self.assertEqual(cross_profile.status_code, 404, cross_profile.text)
        self.assertNotIn("exact", cross_profile.text)


if __name__ == "__main__":
    unittest.main()
