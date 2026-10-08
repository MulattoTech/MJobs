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

    def test_fuzzy_matching_rejects_opposite_positive_and_not_polarity(self):
        self.assertIsNone(self.matcher()(
            "Have you supported production systems?",
            "Have you not supported production systems?",
        ))
        self.assertIsNone(self.matcher()(
            "Can you support production systems?",
            "Can you not support production systems?",
        ))

    def test_fuzzy_matching_rejects_positive_and_never_verb_forms(self):
        self.assertIsNone(self.matcher()(
            "Have you managed Linux servers in production?",
            "Have you never managed Linux servers in production?",
        ))
        self.assertIsNone(self.matcher()(
            "Can you administer Linux servers?",
            "Can't you administer Linux servers?",
        ))

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

    def test_weekend_availability_wording_is_a_curated_ordinary_match(self):
        result = self.matcher()(
            "Are you available to work weekends?",
            "Can you work on weekends?",
        )

        self.assertEqual(result, {"method": "CURATED_SEMANTIC", "score": 0.95})

    def test_weekend_availability_does_not_match_weekday_availability(self):
        self.assertIsNone(self.matcher()(
            "Are you available to work weekends?",
            "Can you work on weekdays?",
        ))

    def test_weekend_availability_does_not_match_opposite_polarity(self):
        self.assertIsNone(self.matcher()(
            "Are you available to work weekends?",
            "Are you unavailable to work weekends?",
        ))

    def test_weekend_semantics_do_not_expand_to_legal_or_sensitive_questions(self):
        matcher = self.matcher()
        weekend = "Are you available to work weekends?"
        for restricted in (
            "Are you legally authorized to work in the United States?",
            "Are you authorized to work in the US?",
            "What is your ethnic origin?",
            "Do you have a disability?",
        ):
            with self.subTest(restricted=restricted):
                self.assertIsNone(matcher(weekend, restricted))

    def test_itsm_acronym_and_expansion_are_a_curated_ordinary_match(self):
        result = self.matcher()(
            "Which ITSM tools have you used?",
            "Which IT service management tools have you used?",
        )

        self.assertEqual(result, {"method": "CURATED_SEMANTIC", "score": 0.95})

    def test_itsm_does_not_expand_to_the_near_miss_service_desk_term(self):
        result = self.matcher()(
            "Which ITSM tools have you used?",
            "Which IT service desk tools have you used?",
        )

        self.assertIsNone(result)

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
