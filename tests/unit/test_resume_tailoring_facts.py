import unittest

from services.documents.errors import PreparationError
from services.documents.tailor import validate_user_rewrites


class ResumeTailoringFactTests(unittest.TestCase):
    def test_reviewed_rewrite_may_rephrase_around_preserved_named_tokens(self):
        cases = [
            (
                "Supported Cisco CCNA routing for 10 sites since 2020.",
                "Maintained Cisco CCNA routing for 10 sites since 2020.",
            ),
            (
                "Administered CompTIA Security+ labs for 4 teams in 2022.",
                "Supported CompTIA Security+ labs for 4 teams in 2022.",
            ),
            (
                "Maintained Microsoft Azure Administrator Associate certification in 2021.",
                "Earned Microsoft Azure Administrator Associate certification in 2021.",
            ),
        ]
        for source, rewrite in cases:
            fact = {
                "fact_id": "experience",
                "kind": "experience",
                "text": source,
                "verified": True,
                "user_approved": True,
                "confidence": 95,
                "source": "User verified",
            }
            with self.subTest(source=source):
                rewritten, _ = validate_user_rewrites([fact], ["experience"], [{
                    "fact_id": "experience", "text": rewrite, "reviewed": True,
                }])
                self.assertEqual(rewritten[0]["text"], rewrite)

    def test_reviewed_rewrite_cannot_drop_named_credential_or_issuer_tokens(self):
        cases = [
            (
                "Supported Cisco CCNA routing for 10 sites since 2020.",
                "Supported network routing for 10 sites since 2020.",
            ),
            (
                "Administered CompTIA Security+ labs for 4 teams in 2022.",
                "Administered security labs for 4 teams in 2022.",
            ),
            (
                "Maintained Microsoft Azure Administrator Associate certification in 2021.",
                "Maintained cloud administration certification in 2021.",
            ),
        ]
        for source, rewrite in cases:
            fact = {
                "fact_id": "experience",
                "kind": "experience",
                "text": source,
                "verified": True,
                "user_approved": True,
                "confidence": 95,
                "source": "User verified",
            }
            with self.subTest(source=source):
                try:
                    validate_user_rewrites([fact], ["experience"], [{
                        "fact_id": "experience", "text": rewrite, "reviewed": True,
                    }])
                except PreparationError as error:
                    self.assertEqual(error.code, "RESUME_REPHRASE_FACTS_CHANGED")
                else:
                    self.fail("reviewed wording dropped a named credential or issuer")


if __name__ == "__main__":
    unittest.main()
