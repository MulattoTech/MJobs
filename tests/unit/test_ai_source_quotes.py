import unittest
from pydantic import ValidationError
from services.ai.consumer import AIJobInput, AIJobEvaluation, validate_job_evidence, _prompt, consume_ai_candidates
from services.ai.cost_controls import AICostPolicy
from services.ai.structured_output import StructuredOutputError


class AISourceQuoteValidationTests(unittest.TestCase):
    def test_exact_fields_duplicates_omitted_tail_and_blank_are_checked(self):
        candidate = AIJobInput('synthetic', 90, 'Python engineer', 'Fixture', 'a' * 8000 + 'OMITTED_PRIVATE_TAIL')
        cases = [
            [{'field': 'description', 'quote': 'OMITTED_PRIVATE_TAIL'}],
            [{'field': 'description', 'quote': 'Python engineer'}],
            [{'field': 'title', 'quote': 'python engineer'}],
            [{'field': 'title', 'quote': 'Python engineer'}] * 2,
            [{'field': 'description', 'quote': ' ' * 8}],
        ]
        for quotes in cases:
            with self.subTest(quotes=quotes), self.assertRaises(StructuredOutputError) as raised:
                validate_job_evidence(candidate, AIJobEvaluation(category='REVIEW', rationale='Advisory', job_evidence=quotes))
            self.assertEqual(str(raised.exception), 'AI_STRUCTURED_OUTPUT_INVALID')
        self.assertNotIn('OMITTED_PRIVATE_TAIL', _prompt(candidate))
        self.assertIn('job_evidence', _prompt(candidate))
        validate_job_evidence(candidate, AIJobEvaluation(category='REVIEW', rationale='Advisory', job_evidence=[{'field':'title','quote':'Python engineer'}]))

    def test_schema_is_bounded_and_closed(self):
        cases = [
            [{'field':'profile','quote':'Python engineer'}],
            [{'field':'title','quote':'short'}],
            [{'field':'title','quote':'x' * 281}],
            [{'field':'title','quote':'Python engineer','confidence':1}],
            [{'field':'title','quote':'Python engineer'}] * 4,
        ]
        for quotes in cases:
            with self.subTest(quotes=quotes), self.assertRaises(ValidationError):
                AIJobEvaluation(category='REVIEW', rationale='Advisory', job_evidence=quotes)

    def test_non_durable_consumer_enforces_same_evidence_contract(self):
        class Router:
            def generate_structured(self, **kwargs):
                return AIJobEvaluation(category='STRONG', rationale='Advisory', job_evidence=[{'field':'title','quote':'Unsupported claim'}])
        with self.assertRaises(StructuredOutputError):
            consume_ai_candidates([AIJobInput('synthetic',90,'Python engineer','Fixture','Python SQL')], AICostPolicy(), Router())
