"""Connected, profile-scoped API coverage for advisory answer suggestions."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationAnswerMatchingApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-answer-match-api-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE, answers=[
            {
                "id": "exact-experience",
                "question": "How many years of professional experience have you had?",
                "text": "I have five years of synthetic professional experience.",
                "source": "Synthetic user-authored example",
                "verified": True,
                "user_approved": True,
                "confidence": 95,
            }, {
                "id": "fuzzy-experience",
                "question": "How many years of professional experience do you have?",
                "text": "A synthetic answer for a fuzzy wording match.",
                "source": "Synthetic user-authored example",
                "verified": True,
                "user_approved": True,
                "confidence": 94,
            }, {
                "id": "semantic-itsm",
                "question": "Which ITSM tools have you used?",
                "text": "I have used synthetic service management tools.",
                "source": "Synthetic user-authored example",
                "verified": True,
                "user_approved": True,
                "confidence": 93,
            }, {
                "id": "unapproved-experience",
                "question": "How many years of professional experience have you had?",
                "text": "Synthetic response that has not been approved.",
                "source": "Synthetic user-authored example",
                "verified": True,
                "user_approved": False,
                "confidence": 100,
            }, {
                "id": "low-confidence-experience",
                "question": "How many years of professional experience have you had?",
                "text": "Synthetic response below the reuse confidence threshold.",
                "source": "Synthetic user-authored example",
                "verified": True,
                "user_approved": True,
                "confidence": 79,
            }, {
                "id": "medical-question",
                "question": "Do you have a medical condition?",
                "text": "Synthetic medical answer that cannot be reused.",
                "source": "Synthetic user-authored example",
                "verified": True,
                "user_approved": True,
                "confidence": 100,
            },
        ]))
        self.store.save_profile(dict(
            PROFILE, id="other", name="Other fixture", email="other@example.test", answers=[]))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs("fixture")[0]
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        from apps.api.application_answer_matching import create_application_answer_matching_router
        self.client.app.include_router(create_application_answer_matching_router(self.store))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _url(self, profile_id="fixture", application_id=None):
        application_id = application_id or self.application_id
        return f"/profiles/{profile_id}/applications/{application_id}/answers/match"

    def _state_digest(self):
        with self.store.engine.connect() as connection:
            rows = {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }
        files = sorted(
            (path.relative_to(self.root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in self.root.rglob("*") if path.is_file()
        )
        return rows, files

    def test_exact_and_fuzzy_candidates_are_ranked_reviewed_and_advisory(self):
        question = "How many years of professional experience have you had?"
        aligned_profile = dict(
            PROFILE,
            id="aligned",
            name="Aligned answer fixture",
            email="aligned@example.test",
            answers=[
                {
                    "id": "exact-experience",
                    "question": "How many years of professional experience have you had?",
                    "text": "A shared synthetic answer for comparison.",
                    "source": "Synthetic user-authored example",
                    "verified": True,
                    "user_approved": True,
                    "confidence": 95,
                }, {
                    "id": "fuzzy-experience",
                    "question": "How many years of professional experience do you have?",
                    "text": "A shared synthetic answer for comparison.",
                    "source": "Synthetic user-authored example",
                    "verified": True,
                    "user_approved": True,
                    "confidence": 94,
                },
            ],
        )
        self.store.save_profile(aligned_profile)
        job = self.store.jobs("fixture")[0]
        aligned_application_id = prepare(
            self.store, job["id"], "aligned", self.root,
        )
        before = self._state_digest()

        response = self.client.post(
            self._url("aligned", aligned_application_id), json={"question": question},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "aligned")
        self.assertEqual(body["application_id"], aligned_application_id)
        self.assertEqual(body["question"], question)
        self.assertFalse(body["review_gate"]["review_required"])
        self.assertIsNone(body["review_gate"]["reason"])
        candidates = body["candidates"]
        self.assertEqual([item["answer_id"] for item in candidates],
                         ["exact-experience", "fuzzy-experience"])
        self.assertEqual([item["method"] for item in candidates],
                         ["EXACT_NORMALIZATION", "FUZZY"])
        self.assertEqual([item["score"] for item in candidates], [1.0, 0.778])
        for candidate in candidates:
            self.assertTrue(candidate["verified"])
            self.assertTrue(candidate["user_approved"])
            self.assertGreaterEqual(candidate["confidence"], 80)
            self.assertTrue(candidate["source"])
            self.assertTrue(candidate["explanation"])
        self.assertNotIn("selected", body)
        self.assertNotIn("approved", body)
        self.assertNotIn("export", body)
        self.assertEqual(self._state_digest(), before)

    def test_conflicting_answer_values_are_review_gated_and_not_returned(self):
        before = self._state_digest()

        response = self.client.post(self._url(), json={
            "question": "How many years of professional experience have you had?",
        })

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["review_gate"]["reason"], "CONFLICTING_ANSWERS")
        self.assertTrue(body["review_gate"]["review_required"])
        self.assertEqual(body["candidates"], [])
        self.assertNotIn("five years of synthetic", response.text)
        self.assertNotIn("fuzzy wording match", response.text)
        self.assertEqual(self._state_digest(), before)

    def test_curated_semantic_match_has_fixed_safe_explanation(self):
        response = self.client.post(self._url(), json={
            "question": "Which IT service management tools have you used?",
        })

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([item["answer_id"] for item in body["candidates"]], ["semantic-itsm"])
        self.assertEqual(body["candidates"][0]["method"], "CURATED_SEMANTIC")
        self.assertEqual(body["candidates"][0]["score"], 0.95)
        self.assertEqual(body["candidates"][0]["explanation"],
                         "The saved question matches a curated ordinary paraphrase.")

    def test_cross_profile_application_and_missing_profile_fail_closed(self):
        before = self._state_digest()
        cross_profile = self.client.post(self._url("other"), json={
            "question": "How many years of professional experience have you had?",
        })
        self.assertEqual(cross_profile.status_code, 404, cross_profile.text)
        self.assertNotIn("exact-experience", cross_profile.text)

        missing_profile = self.client.post(self._url("missing-profile"), json={
            "question": "How many years of professional experience have you had?",
        })
        self.assertEqual(missing_profile.status_code, 404, missing_profile.text)
        self.assertEqual(self._state_digest(), before)

    def test_unsupported_invalid_and_sensitive_questions_never_suggest(self):
        unsupported = self.client.post(self._url(), json={
            "question": "Describe your favorite fictional dessert.",
        })
        self.assertEqual(unsupported.status_code, 200, unsupported.text)
        self.assertEqual(unsupported.json()["candidates"], [])

        saved_sensitive_near_match = self.client.post(self._url(), json={
            "question": "Do you have a condition?",
        })
        self.assertEqual(saved_sensitive_near_match.status_code, 200,
                         saved_sensitive_near_match.text)
        self.assertEqual(saved_sensitive_near_match.json()["candidates"], [])

        before = self._state_digest()
        for body in ({}, {"question": ""}, {"question": "   "}):
            with self.subTest(body=body):
                invalid = self.client.post(self._url(), json=body)
                self.assertEqual(invalid.status_code, 422, invalid.text)

        for question in (
            "Are you legally authorized to work in the United States?",
            "Do you have a medical condition?",
            "What is your ethnicity?",
        ):
            with self.subTest(question=question):
                sensitive = self.client.post(self._url(), json={"question": question})
                self.assertEqual(sensitive.status_code, 422, sensitive.text)
                self.assertNotIn("medical-question", sensitive.text)
        self.assertEqual(self._state_digest(), before)


if __name__ == "__main__":
    unittest.main()
