import unittest

from services.applications.mock_answer_bindings import (
    bind_mock_answer_slots,
    preflight_profile_answers,
)


def answer(answer_id, question="Country", **overrides):
    return {
        "id": answer_id,
        "question": question,
        "text": "United States",
        "source": "Reviewed local fixture",
        "verified": True,
        "user_approved": True,
        "confidence": 90,
        **overrides,
    }


class MockAnswerBindingTests(unittest.TestCase):
    def test_random_id_binds_only_to_one_exact_known_question(self):
        result = bind_mock_answer_slots([answer("ui-random-country-id")])

        self.assertEqual(set(result), {"country"})
        self.assertEqual(result["country"].id, "ui-random-country-id")

    def test_legacy_fixed_id_without_question_remains_eligible(self):
        result = bind_mock_answer_slots([answer("country", question=None)])

        self.assertEqual(set(result), {"country"})
        self.assertEqual(result["country"].text, "United States")

    def test_legacy_fixed_id_with_wrong_question_is_not_a_fallback(self):
        candidate = answer("country", question="What is your preferred shift?")
        result = bind_mock_answer_slots([candidate])

        self.assertNotIn("country", result)
        preflight = preflight_profile_answers({"answers": [candidate]}, result)
        self.assertEqual(preflight["answers"], [])

    def test_duplicate_exact_candidates_are_ambiguous(self):
        result = bind_mock_answer_slots([
            answer("country", question=None), answer("ui-country-2"),
        ])

        self.assertNotIn("country", result)

    def test_ids_reused_across_different_slots_are_rejected(self):
        result = bind_mock_answer_slots([
            answer("remote", question="Country"),
            answer("remote", question=None, text="yes"),
        ])

        self.assertEqual(result, {})

    def test_unapproved_unverified_missing_provenance_or_answer_is_rejected(self):
        cases = (
            {"user_approved": False},
            {"verified": False},
            {"source": ""},
            {"text": "  "},
        )
        for change in cases:
            with self.subTest(change=change):
                self.assertNotIn("country", bind_mock_answer_slots([answer("random", **change)]))

    def test_confidence_below_80_and_nonfinite_confidence_are_rejected(self):
        for confidence in (79, float("nan"), float("inf")):
            with self.subTest(confidence=confidence):
                self.assertNotIn("country", bind_mock_answer_slots([
                    answer("random", confidence=confidence),
                ]))

    def test_confidence_at_80_is_eligible(self):
        self.assertIn("country", bind_mock_answer_slots([
            answer("random", confidence=80),
        ]))

    def test_answer_id_must_fit_preflight_identifier_contract(self):
        self.assertNotIn("country", bind_mock_answer_slots([
            answer("bad answer id"),
        ]))

    def test_exact_normalization_is_allowed_but_near_match_is_not_fuzzy(self):
        exact = bind_mock_answer_slots([answer("exact", question=" COUNTRY! ")])
        near = bind_mock_answer_slots([answer("near", question="Country preference")])

        self.assertIn("country", exact)
        self.assertNotIn("country", near)

    def test_sensitive_and_legal_questions_do_not_bind_to_any_mock_slot(self):
        for question in (
            "What is your gender?",
            "Are you eligible to work?",
            "Do you have a disability?",
        ):
            with self.subTest(question=question):
                self.assertEqual(bind_mock_answer_slots([
                    answer("random-sensitive", question=question),
                ]), {})


if __name__ == "__main__":
    unittest.main()
