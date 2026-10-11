"""Deterministic local email status classification without content retention."""
from email.message import EmailMessage
from email import policy
import hashlib
import unittest

from services.tracking.email_status import classify_email_status


class EmailStatusTests(unittest.TestCase):
    def test_explicit_status_messages_map_to_fixed_categories_and_statuses(self):
        cases = (
            ("Thank you for applying. We have received your application.",
             "CONFIRMATION", "APPLIED", "EXPLICIT_CONFIRMATION"),
            ("We regret to inform you that we will not be moving forward with your application.",
             "REJECTION", "REJECTED", "EXPLICIT_REJECTION"),
            ("We would like to invite you to an interview for the position.",
             "INTERVIEW_INVITATION", "INTERVIEW", "EXPLICIT_INTERVIEW_INVITATION"),
            ("I am a recruiter reaching out about a role that may fit your experience.",
             "RECRUITER_OUTREACH", "RECRUITER_CONTACT", "EXPLICIT_RECRUITER_OUTREACH"),
            ("We are pleased to offer you the position.",
             "OFFER", "OFFER", "EXPLICIT_OFFER"),
        )
        for content, category, suggested_status, reason in cases:
            with self.subTest(category=category):
                result = classify_email_status(content)
                self.assertEqual(result["category"], category)
                self.assertEqual(result["suggested_status"], suggested_status)
                self.assertEqual(result["reason_codes"], [reason])

    def test_unknown_negated_conditional_and_conflicting_content_never_suggest_status(self):
        cases = (
            ("Thanks for your interest. We will be in touch if there is an update.",
             "UNKNOWN", "NO_EXPLICIT_SIGNAL"),
            ("This message is not an offer; no decision has been made.",
             "AMBIGUOUS", "NEGATED_SIGNAL"),
            ("If selected, we may invite you to an interview.",
             "AMBIGUOUS", "CONDITIONAL_SIGNAL"),
            ("Your application was received, but we have decided not to move forward.",
             "AMBIGUOUS", "CONFLICTING_SIGNALS"),
        )
        for content, category, reason in cases:
            with self.subTest(category=category, reason=reason):
                result = classify_email_status(content)
                self.assertEqual(result["category"], category)
                self.assertIsNone(result["suggested_status"])
                self.assertIn(reason, result["reason_codes"])

    def test_result_contains_only_hash_and_fixed_classification_fields(self):
        content = "Application received for alex.private@example.test."

        result = classify_email_status(content)

        self.assertEqual(set(result), {
            "content_sha256", "category", "suggested_status", "reason_codes",
        })
        self.assertEqual(result["content_sha256"], hashlib.sha256(content.encode("utf-8")).hexdigest())
        serialized = repr(result)
        self.assertNotIn(content, serialized)
        self.assertNotIn("alex.private", serialized)
        self.assertNotIn("subject", result)
        self.assertNotIn("body", result)

    def test_eml_uses_plain_text_and_subject_but_ignores_html_and_attachments(self):
        message = EmailMessage(policy=policy.default)
        message["From"] = "synthetic@example.test"
        message["To"] = "private@example.test"
        message["Subject"] = "Interview invitation"
        message.set_content("We would like to invite you to interview next week.")
        message.add_alternative(
            '<html><body>We are pleased to offer you the job.</body></html>',
            subtype="html",
        )
        message.add_attachment(b"We reject your application", maintype="text", subtype="plain",
                               filename="attachment.txt")
        eml = message.as_string()

        result = classify_email_status(eml, format="eml")

        self.assertEqual(result["category"], "INTERVIEW_INVITATION")
        self.assertEqual(result["suggested_status"], "INTERVIEW")
        self.assertEqual(result["content_sha256"], hashlib.sha256(eml.encode("utf-8")).hexdigest())
        for private_value in ("synthetic@example.test", "private@example.test", "We reject your application"):
            self.assertNotIn(private_value, repr(result))

    def test_html_only_eml_is_unknown_and_never_interprets_html(self):
        message = EmailMessage(policy=policy.default)
        message["Subject"] = "Application update"
        message.set_content(" ")
        message.add_alternative("<p>We are pleased to offer you a job.</p>", subtype="html")

        result = classify_email_status(message.as_string(), format="eml")

        self.assertEqual(result["category"], "UNKNOWN")
        self.assertIsNone(result["suggested_status"])

    def test_input_format_and_size_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "EMAIL_FORMAT_INVALID"):
            classify_email_status("test", format="html")
        with self.assertRaisesRegex(ValueError, "EMAIL_CONTENT_INVALID"):
            classify_email_status(b"not text")
        with self.assertRaisesRegex(ValueError, "EMAIL_CONTENT_TOO_LARGE"):
            classify_email_status("x" * (256 * 1024 + 1))

    def test_repeated_classification_is_deterministic_and_never_uses_network(self):
        content = "Subject: Offer\n\nWe are pleased to offer you the role. https://invalid.example/track"

        first = classify_email_status(content)
        second = classify_email_status(content)

        self.assertEqual(first, second)
        self.assertEqual(first["suggested_status"], "OFFER")
        self.assertNotIn("https://", repr(first))


if __name__ == "__main__":
    unittest.main()
