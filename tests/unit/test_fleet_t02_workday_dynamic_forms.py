"""Team 2 regressions for live-DOM Workday question review snapshots."""
import json
import unittest

from services.applications.adapters.workday_forms import (
    analyze_workday_form_html,
    validate_workday_form_html,
)
from services.applications.adapters.workday import WorkdayApplicationAdapter
from services.applications.form_review_providers import analyze_application_form


class WorkdayDynamicFormSnapshotTests(unittest.TestCase):
    route = "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Engineer_R12345"

    def test_aria_controlled_dependent_questions_are_flagged_and_value_free(self):
        markup = (
            '<form><label for="country">Country</label>'
            '<select id="country" aria-controls="region-question" '
            'aria-expanded="true"><option>US</option></select>'
            '<section id="region-question"><label for="region">Region</label>'
            '<input id="region" name="region" value="PRIVATE_REGION"></section>'
            '</form>'
        )

        result = analyze_workday_form_html(markup)

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["fields"][1]["conditional"], True)
        self.assertEqual(result["fields"][1]["conditional_on"], "country")
        self.assertIsNone(result["fields"][0]["conditional"])
        self.assertNotIn("PRIVATE_REGION", json.dumps(result))

    def test_indexed_repeated_experience_rows_require_manual_review(self):
        result = analyze_workday_form_html(
            '<form><input id="employer-0" name="experience[0].employer">'
            '<input id="employer-1" name="experience[1].employer"></form>'
        )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(
            [field["repeatable_group"] for field in result["fields"]],
            ["experience.employer", "experience.employer"],
        )
        self.assertTrue(all(field["repeatable"] for field in result["fields"]))

    def test_duplicate_text_control_names_are_recognized_as_repeated_rows(self):
        result = analyze_workday_form_html(
            '<form><input id="employer-one" name="experience.employer">'
            '<input id="employer-two" name="experience.employer"></form>'
        )

        self.assertTrue(all(field["repeatable"] for field in result["fields"]))
        self.assertEqual(
            [field["repeatable_group"] for field in result["fields"]],
            ["experience.employer", "experience.employer"],
        )

    def test_changed_questions_invalidate_html_snapshot(self):
        original = (
            '<form><label for="email">Email</label>'
            '<input id="email" name="email" type="email"></form>'
        )
        expected = analyze_workday_form_html(original)

        changed = validate_workday_form_html(
            '<form><label for="phone">Phone</label>'
            '<input id="phone" name="phone" type="tel"></form>', expected,
        )

        self.assertEqual(changed["status"], "UNKNOWN")
        self.assertEqual(changed["reason"], "FORM_CHANGED")
        self.assertEqual(changed["fields"], [])

    def test_public_workday_dispatcher_marks_aria_dependent_questions_for_review(self):
        report = analyze_application_form(
            "workday",
            '<form><label for="country">Country</label>'
            '<select id="country" aria-controls="region-question"></select>'
            '<section id="region-question"><label for="region">Region</label>'
            '<input id="region" name="region" value="PRIVATE_REGION"></section></form>',
            route_url=self.route,
        )

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["reason"], "WORKDAY_DYNAMIC_SEMANTICS_UNOBSERVED")
        self.assertNotIn("PRIVATE_REGION", json.dumps(report))

    def test_answer_only_dom_changes_do_not_leak_or_invalidate_question_snapshot(self):
        expected = analyze_workday_form_html(
            '<form><label for="email">Email</label>'
            '<input id="email" name="email" type="email" value="OLD_PRIVATE"></form>'
        )

        current = validate_workday_form_html(
            '<form><label for="email">Email</label>'
            '<input id="email" name="email" type="email" value="NEW_PRIVATE"></form>',
            expected,
        )

        self.assertEqual(current, expected)
        self.assertNotIn("PRIVATE", json.dumps(current))

    def test_adapter_page_validation_invalidates_changed_real_dom_questions(self):
        adapter = WorkdayApplicationAdapter()
        original = {"fields": [{
            "id": "email", "name": "email", "label": "Email", "type": "email",
            "required": False, "conditional": None, "conditional_on": None,
            "repeatable": None, "repeatable_group": None,
        }]}
        changed = {"fields": [{
            "id": "phone", "name": "phone", "label": "Phone", "type": "tel",
            "required": False, "conditional": None, "conditional_on": None,
            "repeatable": None, "repeatable_group": None,
        }]}

        class Page:
            def __init__(self, metadata):
                self.metadata = metadata

            def locator(self, selector):
                if selector != "form":
                    raise AssertionError("Expected Workday form locator")
                return self

            def evaluate_all(self, script, *, timeout):
                self.script = script
                if "outerHTML" in script or ".value" in script or timeout <= 0:
                    raise AssertionError("Expected bounded value-free DOM metadata projection")
                return self.metadata

        reviewed = adapter.analyze_page(Page(original))
        validation = adapter.validate_page(Page(changed), reviewed)

        self.assertEqual(validation, {"status": "UNKNOWN", "reason": "FORM_CHANGED"})


if __name__ == "__main__":
    unittest.main()
