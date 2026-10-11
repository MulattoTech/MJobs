import json
import unittest

from services.applications.adapters.lever_markup import analyze_lever_markup


class LeverMarkupTests(unittest.TestCase):
    def test_control_summaries_discard_values_and_classify_sensitive_fields(self):
        result = analyze_lever_markup([
            {"tag": "input", "id": "candidate-email", "name": "email",
             "labels": ["Email address"], "input_type": "email",
             "value": "PRIVATE_CURRENT_VALUE", "answer": "PRIVATE_ANSWER"},
            {"tag": "input", "id": "birth-date", "labels": ["Date of birth"],
             "value": "PRIVATE_BIRTH_DATE"},
        ])

        self.assertEqual(result["status"], "PASS")
        self.assertEqual([field["sensitivity"] for field in result["fields"]],
                         ["STANDARD", "SENSITIVE"])
        serialized = json.dumps(result)
        for secret in ("PRIVATE_CURRENT_VALUE", "PRIVATE_ANSWER", "PRIVATE_BIRTH_DATE"):
            self.assertNotIn(secret, serialized)
        self.assertTrue(all("value" not in field and "answer" not in field
                            for field in result["fields"]))

    def test_static_markup_extracts_only_control_metadata_and_label_text(self):
        markup = '''<form><label for="full-name">Full name</label>
          <input id="full-name" name="name" value="PRIVATE_VALUE">
          <label for="country">Country</label><select id="country" name="country">
            <option value="us">United States</option><option value="ca">Canada</option>
          </select><textarea name="cover_note">PRIVATE_ANSWER</textarea></form>'''

        result = analyze_lever_markup(markup)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual([field["labels"] for field in result["fields"]],
                         [["Full name"], ["Country"], []])
        self.assertEqual(result["fields"][1]["options"], ["United States", "Canada"])
        serialized = json.dumps(result)
        self.assertNotIn("PRIVATE_VALUE", serialized)
        self.assertNotIn("PRIVATE_ANSWER", serialized)
        self.assertNotIn('"value"', serialized)

    def test_markup_sensitive_labels_are_classified_without_answer_text(self):
        result = analyze_lever_markup(
            '<form><label for="ssn">Social Security number</label>'
            '<input id="ssn" value="PRIVATE_SSN"></form>'
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["fields"][0]["sensitivity"], "SENSITIVE")
        self.assertNotIn("PRIVATE_SSN", json.dumps(result))

    def test_invalid_or_over_limit_sources_fail_closed(self):
        from services.applications.adapters.lever_markup import MAX_MARKUP_LENGTH

        self.assertEqual(analyze_lever_markup(object())["status"], "UNKNOWN")
        over_limit = analyze_lever_markup("x" * (MAX_MARKUP_LENGTH + 1))
        self.assertEqual(over_limit["status"], "UNKNOWN")
        self.assertEqual(over_limit["fields"], [])
        too_many_controls = analyze_lever_markup([
            {"tag": "input", "name": f"field-{index}"} for index in range(101)
        ])
        self.assertEqual(too_many_controls["status"], "UNKNOWN")
        self.assertEqual(too_many_controls["fields"], [])

    def test_challenge_or_unrepresented_interactive_controls_fail_closed(self):
        challenge = analyze_lever_markup(
            '<form><div class="cf-turnstile">PRIVATE_CHALLENGE</div>'
            '<input id="email" name="email"></form>'
        )
        widget = analyze_lever_markup(
            '<form><input id="email" name="email">'
            '<div role="combobox" aria-label="Private preference"></div></form>'
        )
        editable = analyze_lever_markup(
            '<form><input id="email" name="email">'
            '<div contenteditable aria-label="Private response"></div></form>'
        )
        duplicate_attribute = analyze_lever_markup(
            '<form><input id="email" id="other" name="email"></form>'
        )

        self.assertEqual(challenge["status"], "UNKNOWN")
        self.assertEqual(challenge["reason"], "CHALLENGE_PROTECTED")
        self.assertEqual(widget["status"], "UNKNOWN")
        self.assertEqual(widget["reason"], "UNSUPPORTED_INTERACTIVE_CONTROL")
        self.assertEqual(editable["reason"], "UNSUPPORTED_INTERACTIVE_CONTROL")
        self.assertEqual(duplicate_attribute["status"], "UNKNOWN")
        self.assertNotIn("PRIVATE_CHALLENGE", json.dumps(challenge))
        self.assertNotIn("Private preference", json.dumps(widget))

    def test_markup_requires_exactly_one_form(self):
        self.assertEqual(analyze_lever_markup(
            '<input id="email" name="email">',
        )["reason"], "NO_FORM")
        multiple = analyze_lever_markup(
            '<form><input id="email" name="email"></form>'
            '<form><input id="newsletter" name="email"></form>',
        )
        self.assertEqual(multiple["status"], "UNKNOWN")
        self.assertEqual(multiple["reason"], "AMBIGUOUS_FORM")
        self.assertEqual(multiple["fields"], [])
        outside = analyze_lever_markup(
            '<form><input id="email" name="email"></form>'
            '<input id="newsletter" name="newsletter_email">',
        )
        self.assertEqual(outside["status"], "UNKNOWN")
        self.assertEqual(outside["reason"], "AMBIGUOUS_FORM")


if __name__ == "__main__":
    unittest.main()
