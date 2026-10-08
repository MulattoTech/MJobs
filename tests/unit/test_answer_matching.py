"""Local, bounded question matching for reusable application answers."""
import unittest

from services.documents import answers


class AnswerMatchingTests(unittest.TestCase):
    def matcher(self):
        matcher = getattr(answers, "match_answer_question", None)
        self.assertTrue(callable(matcher), "answer question matcher must be implemented")
        return matcher

    def test_exact_normalization_matches_only_case_and_punctuation_variants(self):
        result = self.matcher()("How many years of experience?", "HOW MANY YEARS OF EXPERIENCE!")

        self.assertEqual(result, {"method": "EXACT_NORMALIZATION", "score": 1.0})

    def test_fuzzy_method_captures_small_edits_with_a_bounded_score(self):
        result = self.matcher()(
            "How many years of professional experience have you had?",
            "How many years of professional experience do you have?",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["method"], "FUZZY")
        self.assertGreaterEqual(result["score"], 0.75)
        self.assertLess(result["score"], 0.95)

    def test_configured_match_confidence_threshold_filters_fuzzy_matches(self):
        question = "How many years of professional experience have you had?"
        saved_question = "How many years of professional experience do you have?"

        self.assertIsNone(self.matcher()(question, saved_question, minimum_confidence=0.80))
        self.assertEqual(self.matcher()(question, saved_question, minimum_confidence=0.75)["score"], 0.778)

    def test_match_threshold_boundary_uses_the_visible_rounded_score(self):
        result = self.matcher()(
            "How many years of professional experience have you had?",
            "How many years of professional experience do you have?",
            minimum_confidence=0.778,
        )

        self.assertEqual(result, {"method": "FUZZY", "score": 0.778})

    def test_curated_semantic_synonyms_are_distinct_from_fuzzy_matching(self):
        result = self.matcher()(
            "What coding languages have you worked with?",
            "Which programming languages have you used?",
        )

        self.assertEqual(result, {"method": "CURATED_SEMANTIC", "score": 0.95})

    def test_cloud_provider_and_platform_wording_is_a_curated_ordinary_match(self):
        result = self.matcher()(
            "What cloud providers have you used?",
            "Which cloud platforms have you worked with?",
        )

        self.assertEqual(result, {"method": "CURATED_SEMANTIC", "score": 0.95})

    def test_unrelated_near_miss_does_not_match(self):
        result = self.matcher()(
            "How many years of education?",
            "How many years of professional experience do you have?",
        )

        self.assertIsNone(result)

    def test_sensitive_or_legal_prompts_are_never_reusable_matches(self):
        result = self.matcher()(
            "Are you legally authorized for employment in the United States?",
            "Are you authorized to work in the US?",
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
