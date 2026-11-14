"""Value-free, deterministic iCIMS static review projection tests."""
import json
import unittest

from services.applications.form_review_providers import analyze_application_form


ROUTE = "https://careers-example.icims.com/jobs/6534/hr-it--project-manager/job"


class ICIMSFormReviewTests(unittest.TestCase):
    def test_known_visible_contact_control_is_projected_without_values_or_text(self):
        report = analyze_application_form(
            "icims",
            '<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email" value="PRIVATE">'
            '</form>',
            route_url=ROUTE,
        )
        self.assertEqual(report, {
            "status": "ANALYZED", "reason": None,
            "fields": [{"id": "email", "name": "email", "type": "email",
                        "required": False, "review_reason": None}],
        })
        encoded = json.dumps(report)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("Email address", encoded)

    def test_required_known_fields_still_require_human_review(self):
        report = analyze_application_form(
            "icims", '<form><label for="email">Email</label>'
            '<input id="email" name="email" type="email" required></form>',
            route_url=ROUTE,
        )
        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["fields"][0]["review_reason"], "REQUIRED_FIELD_REVIEW")

    def test_custom_required_and_unsafe_fields_require_review_and_do_not_leak(self):
        html = (
            '<form><label for="experience">Describe work eligibility</label>'
            '<input id="experience" name="custom_field" required value="SECRET">'
            '<input id="dob\u200b" name="unsafe" type="text"></form>'
        )
        report = analyze_application_form("icims", html, route_url=ROUTE)
        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertTrue(all(field["review_reason"] for field in report["fields"]))
        encoded = json.dumps(report)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("Describe work eligibility", encoded)
        self.assertNotIn("dob", encoded)

    def test_unknown_route_and_malformed_markup_fail_closed_with_fixed_report(self):
        self.assertEqual(analyze_application_form(
            "icims", '<form></form>', route_url="https://evil.example/jobs/1/job",
        ), {"status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": []})
        self.assertEqual(analyze_application_form(
            "icims", object(), route_url=ROUTE,
        ), {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []})


if __name__ == "__main__":
    unittest.main()
