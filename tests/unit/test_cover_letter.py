import unittest

from services.documents.cover_letter import build_cover_letter
from services.documents.errors import PreparationError


PROFILE = {"name": "Alex Example"}
JOB = {
    "title": "Platform Engineer",
    "company": "Example Systems",
    "description": "Responsibilities:\nBuild Python services for reporting.\nMaintain SQL data pipelines.",
    "skills": ["Python", "SQL"],
    "attributes": {"skill_evidence": "structured_required_skills"},
}
FACTS = [
    {"fact_id": "python", "kind": "skill", "text": "Python", "source": "reviewed resume",
     "verified": True, "user_approved": True, "confidence": 100},
    {"fact_id": "sql", "kind": "skill", "text": "SQL", "source": "reviewed resume",
     "verified": True, "user_approved": True, "confidence": 100},
    {"fact_id": "pending", "kind": "skill", "text": "Kubernetes", "source": "import",
     "verified": False, "user_approved": False, "confidence": 20},
]


class CoverLetterTests(unittest.TestCase):
    def test_job_specific_letter_uses_only_approved_facts_and_exact_posting_evidence(self):
        paragraphs, context = build_cover_letter(PROFILE, JOB, FACTS)

        rendered = "\n".join(paragraphs)
        self.assertIn("Platform Engineer", rendered)
        self.assertIn("Example Systems", rendered)
        self.assertIn("Build Python services for reporting.", rendered)
        self.assertIn("Python", rendered)
        self.assertNotIn("Kubernetes", rendered)
        self.assertEqual(context["mode"], "JOB_SPECIFIC_APPROVED_FACTS")
        self.assertEqual(context["fact_ids"], ["python", "sql"])
        self.assertEqual(context["skills"], ["Python", "SQL"])

    def test_style_and_length_are_explicit_and_deterministic(self):
        outputs = {
            (style, length): build_cover_letter(PROFILE, JOB, FACTS, style, length)[0]
            for style in ("professional", "warm", "technical", "direct", "conversational", "formal")
            for length in ("very short", "concise", "normal", "detailed")
        }
        self.assertEqual(outputs, {
            (style, length): build_cover_letter(PROFILE, JOB, FACTS, style, length)[0]
            for style in ("professional", "warm", "technical", "direct", "conversational", "formal")
            for length in ("very short", "concise", "normal", "detailed")
        })
        self.assertGreater(len(" ".join(outputs[("professional", "detailed")]).split()),
                           len(" ".join(outputs[("professional", "very short")]).split()))
        self.assertNotEqual(outputs[("professional", "normal")], outputs[("warm", "normal")])

    def test_missing_job_evidence_or_matching_approved_facts_blocks_preparation(self):
        with self.assertRaises(PreparationError) as missing_responsibility:
            build_cover_letter(PROFILE, {**JOB, "description": "Python and SQL are useful."}, FACTS)
        self.assertEqual(missing_responsibility.exception.code, "COVER_LETTER_JOB_CONTEXT_MISSING")

        with self.assertRaises(PreparationError) as missing_match:
            build_cover_letter(PROFILE, JOB, [FACTS[-1]])
        self.assertEqual(missing_match.exception.code, "COVER_LETTER_RELEVANT_FACTS_MISSING")

    def test_unsupported_style_is_rejected(self):
        with self.assertRaises(PreparationError) as error:
            build_cover_letter(PROFILE, JOB, FACTS, "overjoyed", "normal")
        self.assertEqual(error.exception.code, "COVER_LETTER_OPTIONS_INVALID")


if __name__ == "__main__":
    unittest.main()
