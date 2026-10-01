import unittest

from cli.live_feedback import redact_url, safe_url, sanitize_interaction, text_fingerprint


class LiveFeedbackTests(unittest.TestCase):
    def test_safe_url_requires_local_http_and_removes_query_fragment(self):
        self.assertEqual(
            safe_url("http://127.0.0.1:18701/jobs?token=secret#section"),
            "http://127.0.0.1:18701/jobs",
        )
        with self.assertRaises(ValueError):
            safe_url("https://example.com/jobs?token=secret")

    def test_redact_url_never_preserves_query_or_fragment(self):
        self.assertEqual(
            redact_url("https://example.com/apply?email=user@example.com#private"),
            "https://example.com/apply",
        )

    def test_form_interaction_never_records_value_or_text(self):
        payload = {
            "type": "change",
            "tag": "input",
            "id": "email",
            "name": "email",
            "role": "textbox",
            "aria_label": "Email",
            "text": "dylan@example.com",
            "value": "dylan@example.com",
            "password": "secret",
        }
        result = sanitize_interaction(payload)
        self.assertNotIn("value", result)
        self.assertNotIn("password", result)
        self.assertNotIn("text", result)
        self.assertEqual(result["name"], "email")

    def test_non_form_click_keeps_only_bounded_element_metadata(self):
        result = sanitize_interaction({
            "type": "click", "tag": "button", "id": "search", "name": "",
            "role": "button", "aria_label": "Search jobs",
            "text": "Search " + ("x" * 500), "value": "must-not-appear",
        })
        self.assertEqual(result["tag"], "button")
        self.assertLessEqual(len(result["text"]), 160)
        self.assertNotIn("value", result)

    def test_diagnostic_text_fingerprint_does_not_expose_original_text(self):
        result = text_fingerprint("private diagnostic string")
        self.assertEqual(result["length"], len("private diagnostic string"))
        self.assertNotIn("private", str(result))


if __name__ == "__main__":
    unittest.main()
