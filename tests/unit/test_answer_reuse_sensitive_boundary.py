"""Sensitive reuse follows the same boundary regardless of the caller."""
from pathlib import Path
import subprocess
import sys
import unittest

from services.documents.answers import match_answer_question


class AnswerReuseSensitiveBoundaryTests(unittest.TestCase):
    def test_sensitive_and_legal_paraphrases_never_become_exact_reuse(self):
        questions = (
            "What is your race or ethnicity?",
            "What is your faith tradition?",
            "What is your gender identity?",
            "Do you have a disability?",
            "Have you served in the armed forces?",
            "What is your national origin?",
            "Are you permitted to work in the United States?",
            "Will you require sponsorship?",
            "Have you ever been arrested?",
            "Are you at least 18 years old?",
            "Do you have conflicts of interest?",
            "What legal certifications do you hold?",
            "What is your expected salary?",
            "Do you have restrictive covenants?",
            "What is your medical history?",
            "What is your social security number?",
        )
        for question in questions:
            with self.subTest(question=question):
                self.assertIsNone(match_answer_question(question, question))
                self.assertIsNone(match_answer_question(question.upper(), question))

    def test_either_side_of_a_high_overlap_pair_can_require_individual_review(self):
        ordinary = "How many years of cloud service experience do you have?"
        sensitive = "How many years of military service experience do you have?"
        for requested, saved in ((ordinary, sensitive), (sensitive, ordinary)):
            with self.subTest(requested=requested, saved=saved):
                self.assertIsNone(match_answer_question(requested, saved))

    def test_ordinary_terms_and_numeric_context_keep_exact_reuse(self):
        for question in (
            "Describe the origin tracking you implemented for data pipelines.",
            "How have you used salary benchmarking reports in workforce analytics?",
            "Have you implemented military-grade encryption for stored data?",
            "Which tools have you used for orchestration service monitoring?",
            "Have you built permission controls for internal services?",
            "How did you maintain 18 servers during the migration?",
        ):
            with self.subTest(question=question):
                self.assertEqual(match_answer_question(question, question), {
                    "method": "EXACT_NORMALIZATION", "score": 1.0,
                })

    def test_ordinary_nearby_wording_still_respects_polarity(self):
        positive = "Have you implemented military-grade encryption for stored data?"
        for negative in (
            "Have you not implemented military-grade encryption for stored data?",
            "Have you never implemented military-grade encryption for stored data?",
            "Haven't you implemented military-grade encryption for stored data?",
        ):
            with self.subTest(negative=negative):
                self.assertIsNone(match_answer_question(positive, negative))
                self.assertIsNone(match_answer_question(negative, positive))

    def test_shared_policy_imports_work_in_both_fresh_process_orders(self):
        root = Path(__file__).resolve().parents[2]
        for first in ("services.documents.answers", "services.answers.review"):
            with self.subTest(first=first):
                script = f"""import importlib
importlib.import_module({first!r})
from services.documents.answers import match_answer_question
from services.answers.review import is_review_sensitive_question
question = 'Have you served in the armed forces?'
assert is_review_sensitive_question(question)
assert match_answer_question(question, question) is None
assert match_answer_question('Which languages have you used?', 'Which languages have you used?')['score'] == 1.0
"""
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script], cwd=root,
                    capture_output=True, text=True, timeout=15, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
