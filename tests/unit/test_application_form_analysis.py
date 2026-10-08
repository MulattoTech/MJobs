"""Bounded, value-free metadata analysis for synthetic application forms."""
import json
import unittest

from services.applications.form_analysis import (
    MAX_CONTROLS,
    MAX_METADATA_LENGTH,
    MAX_OPTIONS,
    analyze_controls,
)


class ApplicationFormAnalysisTests(unittest.TestCase):
    def test_each_supported_metadata_source_is_extracted(self):
        cases = (
            ("labels", ["Legal name"], "Legal name"),
            ("name", "full_name", "full_name"),
            ("id", "applicant-name", "applicant-name"),
            ("placeholder", "Enter your name", "Enter your name"),
            ("aria_label", "Applicant name", "Applicant name"),
            ("input_type", "email", "email"),
            ("nearby_text", "Use the name on your resume", "Use the name on your resume"),
            ("section_heading", "Contact details", "Contact details"),
            ("fieldset_legend", "Applicant identity", "Applicant identity"),
            ("options", ["United States", "Canada"], "Canada"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                result = analyze_controls([{"tag": "select" if field == "options" else "input",
                                           "id": "metadata-source-test", field: value}])
                self.assertEqual(result["status"], "PASS")
                self.assertIn(expected, json.dumps(result))

    def test_metadata_sources_combine_without_capturing_field_values(self):
        result = analyze_controls([{
            "tag": "input", "labels": ["Work email"], "name": "email",
            "id": "contact-email", "placeholder": "name@example.test",
            "aria_label": "Email address", "input_type": "email",
            "nearby_text": "Use an address you check regularly",
            "section_heading": "Contact details", "fieldset_legend": "Applicant",
        }])

        self.assertEqual(result["status"], "PASS")
        field = result["fields"][0]
        self.assertEqual(field["labels"], ["Work email"])
        self.assertEqual(field["name"], "email")
        self.assertEqual(field["id"], "contact-email")
        self.assertEqual(field["placeholder"], "name@example.test")
        self.assertEqual(field["aria_label"], "Email address")
        self.assertEqual(field["input_type"], "email")
        self.assertEqual(field["nearby_text"], "Use an address you check regularly")
        self.assertEqual(field["section_heading"], "Contact details")
        self.assertEqual(field["fieldset_legend"], "Applicant")
        serialized = json.dumps(result)
        self.assertNotIn("value", field)
        self.assertNotIn("PRIVATE_CURRENT_VALUE", serialized)

    def test_sensitive_and_credential_controls_include_classification_only(self):
        result = analyze_controls([
            {"tag": "input", "name": "password", "input_type": "password",
             "labels": ["Account password"]},
            {"tag": "input", "name": "ssn", "labels": ["Social Security number"]},
        ])

        self.assertEqual(result["status"], "PASS")
        self.assertEqual([field["sensitivity"] for field in result["fields"]],
                         ["CREDENTIAL", "SENSITIVE"])
        serialized = json.dumps(result)
        self.assertNotIn("password-value", serialized)
        self.assertNotIn("ssn-value", serialized)
        self.assertTrue(all("value" not in field for field in result["fields"]))

    def test_controls_with_any_value_key_fail_closed_without_echoing_it(self):
        result = analyze_controls([{
            "tag": "input", "name": "password", "input_type": "password",
            "value": "PRIVATE_CURRENT_VALUE",
        }])

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "FORBIDDEN_VALUE_METADATA")
        self.assertEqual(result["fields"], [])
        self.assertNotIn("PRIVATE_CURRENT_VALUE", json.dumps(result))

    def test_duplicate_stable_identifiers_are_marked_ambiguous(self):
        result = analyze_controls([
            {"tag": "input", "id": "duplicate", "name": "given_name", "labels": ["First"]},
            {"tag": "input", "id": "duplicate", "name": "middle_name", "labels": ["Middle"]},
        ])

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "AMBIGUOUS_CONTROL_IDENTITY")
        self.assertTrue(all(field["ambiguous"] for field in result["fields"]))

    def test_malformed_controls_and_metadata_lengths_are_bounded(self):
        malformed = analyze_controls([{"tag": "script", "id": "not-a-control"}])
        too_long = analyze_controls([{"tag": "input", "placeholder": "x" * (MAX_METADATA_LENGTH + 1)}])
        too_many = analyze_controls([{"tag": "input", "name": f"field-{index}"}
                                     for index in range(MAX_CONTROLS + 1)])
        too_many_options = analyze_controls([{"tag": "select", "id": "country",
                                               "options": [f"option-{index}" for index in range(MAX_OPTIONS + 1)]}])

        for result in (malformed, too_long, too_many, too_many_options):
            with self.subTest(result=result):
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["fields"], [])


if __name__ == "__main__":
    unittest.main()
