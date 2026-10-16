"""Synthetic contract tests for value-free Workday form metadata."""
import json
import unittest

from services.applications.adapters.workday_forms import (
    WORKDAY_FORM_CAPABILITIES,
    analyze_workday_form,
    analyze_workday_form_html,
    requires_workday_human_review,
    validate_workday_form,
)


def form(fields):
    return {
        "provider": "workday",
        "schema_version": 1,
        "form_id": "application-form",
        "revision": "revision-1",
        "fields": fields,
    }


def field(field_id="email", **overrides):
    value = {
        "id": field_id,
        "label": "Email address",
        "type": "email",
        "required": True,
        "conditional": False,
        "repeatable": False,
    }
    value.update(overrides)
    return value


class WorkdayFormAnalyzerTests(unittest.TestCase):
    def test_capabilities_are_narrow_and_explicit(self):
        self.assertEqual(WORKDAY_FORM_CAPABILITIES, {
            "metadata_analysis": True,
            "human_review_for_uncertainty": True,
            "page_interaction": False,
            "applicant_value_reading": False,
            "autofill": False,
            "document_upload": False,
            "submission": False,
        })

    def test_supported_required_metadata_is_normalized_deterministically(self):
        metadata = form([field()])

        result = analyze_workday_form(metadata)

        self.assertEqual(result, analyze_workday_form(metadata))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["provider"], "workday")
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["fields"], [{
            "id": "email",
            "label": "Email address",
            "type": "email",
            "required": True,
            "conditional": False,
            "conditional_on": None,
            "repeatable": False,
            "repeatable_group": None,
            "sensitivity": "STANDARD",
            "supported": True,
            "requires_human_review": False,
        }])
        self.assertFalse(requires_workday_human_review(result))

    def test_required_fields_are_reported_without_answer_slots(self):
        result = analyze_workday_form(form([field("first-name", type="text")]))

        self.assertTrue(result["fields"][0]["required"])
        self.assertNotIn("answer", json.dumps(result).casefold())
        self.assertNotIn("value", json.dumps(result).casefold())

    def test_conditional_fields_require_review_and_need_a_known_controller(self):
        result = analyze_workday_form(form([
            field("country", label="Country", type="select"),
            field("region", conditional=True, conditional_on="country"),
        ]))

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertTrue(result["fields"][1]["requires_human_review"])
        self.assertTrue(requires_workday_human_review(result))

        missing_controller = analyze_workday_form(form([field("region", conditional=True)]))
        self.assertEqual(missing_controller["status"], "UNKNOWN")
        self.assertEqual(missing_controller["fields"], [])

    def test_repeated_fields_require_review_and_group_identity(self):
        result = analyze_workday_form(form([field(
            "experience-employer", repeatable=True, repeatable_group="experience",
        )]))

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["fields"][0]["repeatable_group"], "experience")
        self.assertTrue(requires_workday_human_review(result))

        missing_group = analyze_workday_form(form([field("experience-employer", repeatable=True)]))
        self.assertEqual(missing_group["status"], "UNKNOWN")
        self.assertEqual(missing_group["fields"], [])

    def test_sensitive_unknown_and_unsupported_semantics_require_review(self):
        sensitive = analyze_workday_form(form([field(
            "tax-id", label="Social Security number", type="text",
        )]))
        unknown_type = analyze_workday_form(form([field("custom", type="workday-custom-widget")]))

        self.assertEqual(sensitive["status"], "REVIEW_REQUIRED")
        self.assertEqual(sensitive["fields"][0]["sensitivity"], "SENSITIVE")
        self.assertEqual(unknown_type["status"], "REVIEW_REQUIRED")
        self.assertFalse(unknown_type["fields"][0]["supported"])
        self.assertTrue(requires_workday_human_review(unknown_type))

    def test_answer_value_keys_unknown_keys_and_changed_schema_fail_closed(self):
        forbidden = analyze_workday_form(form([field(value="PRIVATE_ANSWER")]))
        unknown = analyze_workday_form(form([field(mystery="metadata")]))
        changed = analyze_workday_form({**form([field()]), "schema_version": 2})

        for result in (forbidden, unknown, changed):
            with self.subTest(result=result):
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["fields"], [])
                self.assertTrue(requires_workday_human_review(result))
        self.assertNotIn("PRIVATE_ANSWER", json.dumps(forbidden))

    def test_duplicate_ids_and_invalid_required_metadata_fail_closed(self):
        duplicate = analyze_workday_form(form([field("same"), field("same", label="Other")]))
        invalid_required = analyze_workday_form(form([field(required="yes")]))

        for result in (duplicate, invalid_required):
            with self.subTest(result=result):
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["fields"], [])

    def test_snapshot_validation_accepts_identical_metadata_and_rejects_changes(self):
        metadata = form([field()])
        expected = analyze_workday_form(metadata)

        self.assertEqual(validate_workday_form(metadata, expected), expected)
        changed_revision = {**metadata, "revision": "revision-2"}
        changed_field = form([field("email", required=False)])
        for changed in (changed_revision, changed_field):
            with self.subTest(changed=changed):
                result = validate_workday_form(changed, expected)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["reason"], "FORM_CHANGED")
                self.assertTrue(requires_workday_human_review(result))

    def test_snapshot_validation_rejects_malformed_prior_analysis_without_echoing_data(self):
        result = validate_workday_form(form([field()]), {
            "provider": "workday", "status": "PASS", "fields": [],
            "answer": "PRIVATE_ANSWER",
        })

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("PRIVATE_ANSWER", json.dumps(result))

    def test_unobserved_dynamic_semantics_are_not_normalized_to_false(self):
        result = analyze_workday_form(form([field(
            "email", conditional=None, repeatable=None,
        )]))

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIsNone(result["fields"][0]["conditional"])
        self.assertIsNone(result["fields"][0]["repeatable"])
        self.assertTrue(result["fields"][0]["requires_human_review"])

    def test_workday_html_analyzer_ignores_input_option_and_textarea_values(self):
        result = analyze_workday_form_html(
            '<form><label for="email">Email</label>'
            '<input id="email" name="email" type="email" value="PRIVATE_INPUT">'
            '<label for="choice">Choice</label><select id="choice" name="choice">'
            '<option value="PRIVATE_OPTION" selected>PRIVATE_OPTION_LABEL</option></select>'
            '<label for="note">Note</label><textarea id="note" name="note">PRIVATE_ANSWER</textarea>'
            '</form>'
        )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        serialized = json.dumps(result)
        for secret in ("PRIVATE_INPUT", "PRIVATE_OPTION", "PRIVATE_OPTION_LABEL", "PRIVATE_ANSWER"):
            self.assertNotIn(secret, serialized)
        self.assertIsNone(result["fields"][0]["conditional"])
        self.assertIsNone(result["fields"][0]["repeatable"])

    def test_workday_html_analyzer_fails_closed_on_deeply_nested_labels(self):
        markup = (
            "<form>" + "<label>" * 33 + "Email" + "</label>" * 33 + "</form>"
        )

        result = analyze_workday_form_html(markup)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "INVALID_WORKDAY_MARKUP")
        self.assertEqual(result["fields"], [])

    def test_workday_html_analyzer_fails_closed_on_excessive_markup_events(self):
        text_fragment = "<span>" * 8 + "x" + "</span>" * 8
        markup = "<form><label>" + text_fragment * 250 + "</label></form>"

        result = analyze_workday_form_html(markup)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "INVALID_WORKDAY_MARKUP")
        self.assertEqual(result["fields"], [])


if __name__ == "__main__":
    unittest.main()
