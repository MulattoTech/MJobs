import json
import unittest
from unittest.mock import patch

from services.applications import form_review_providers
from services.applications.form_review_providers import (
    analyze_application_form,
    normalize_application_route,
)
from services.applications.workday_dynamic_review import validate_workday_dynamic_review


class ApplicationFormReviewProvidersTests(unittest.TestCase):
    greenhouse_route = "https://job-boards.greenhouse.io/example/jobs/1234"
    lever_route = "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000"
    recruitee_route = "https://example.recruitee.com/o/software-engineer"
    workday_route = (
        "https://example.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_R123"
    )

    def test_normalizes_supported_routes_with_existing_adapters(self):
        self.assertEqual(normalize_application_route("GREENHOUSE", self.greenhouse_route), {
            "provider": "greenhouse",
            "tenant": "example",
            "requisition": "1234",
            "url": "https://boards.greenhouse.io/example/jobs/1234",
        })
        self.assertEqual(normalize_application_route("lever", self.lever_route), {
            "provider": "lever",
            "tenant": "example",
            "requisition": "123e4567-e89b-12d3-a456-426614174000",
            "url": self.lever_route,
        })

    def test_normalizes_recruitee_route_through_provider_registry(self):
        self.assertEqual(normalize_application_route("Recruitee", self.recruitee_route), {
            "provider": "recruitee",
            "tenant": "example",
            "requisition": "software-engineer",
            "url": self.recruitee_route,
        })

    def test_dispatches_recruitee_form_review_and_fails_closed_for_custom_fields(self):
        analyzed = analyze_application_form(
            "recruitee",
            '<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email" value="PRIVATE_VALUE"></form>',
            route_url=self.recruitee_route,
        )
        review_required = analyze_application_form(
            "recruitee",
            '<form><label for="q">Describe your experience</label>'
            '<textarea id="q" name="custom_question">PRIVATE_ANSWER</textarea></form>',
            route_url=self.recruitee_route,
        )

        self.assertEqual(analyzed, {
            "status": "ANALYZED", "reason": None,
            "fields": [{"id": "email", "name": "email", "type": "email",
                        "required": False, "review_reason": None}],
        })
        self.assertNotIn("PRIVATE_VALUE", json.dumps(analyzed))
        self.assertEqual(review_required["status"], "REVIEW_REQUIRED")
        self.assertEqual(review_required["fields"][0]["review_reason"], "UNKNOWN_QUESTION")
        self.assertNotIn("PRIVATE_ANSWER", json.dumps(review_required))

    def test_recruitee_dispatch_requires_review_for_prompt_not_in_its_allowlist(self):
        report = analyze_application_form(
            "recruitee",
            '<form><label for="phone_type">Phone type</label>'
            '<input id="phone_type" name="phone_type" type="text"></form>',
            route_url=self.recruitee_route,
        )

        self.assertEqual(report, {
            "status": "REVIEW_REQUIRED",
            "reason": "FORM_REQUIRES_HUMAN_REVIEW",
            "fields": [{
                "id": "phone_type", "name": "phone_type", "type": "text",
                "required": False, "review_reason": "UNKNOWN_QUESTION",
            }],
        })

    def test_analyzes_greenhouse_html_without_returning_values_or_page_text(self):
        html = (
            '<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email" required value="PRIVATE_VALUE">'
            '</form>'
        )

        report = analyze_application_form("greenhouse", html, route_url=self.greenhouse_route)

        self.assertEqual(report, {
            "status": "ANALYZED", "reason": None,
            "fields": [{"id": "email", "name": "email", "type": "email",
                        "required": True, "review_reason": None}],
        })
        self.assertNotIn("PRIVATE_VALUE", json.dumps(report))
        self.assertNotIn("Email address", json.dumps(report))

    def test_analyzes_lever_markup_into_the_same_value_free_report_shape(self):
        markup = (
            '<form><label for="name">Full name</label>'
            '<input id="name" name="full_name" type="text" value="PRIVATE_VALUE">'
            '</form>'
        )

        report = analyze_application_form("lever", markup, route_url=self.lever_route)

        self.assertEqual(report, {
            "status": "ANALYZED", "reason": None,
            "fields": [{"id": "name", "name": "full_name", "type": "text",
                        "required": False, "review_reason": None}],
        })
        self.assertNotIn("PRIVATE_VALUE", json.dumps(report))
        self.assertNotIn("Full name", json.dumps(report))

    def test_workday_dispatch_calls_its_own_local_html_analyzer(self):
        html = '<form><input id="email" name="email" type="email" required></form>'
        with patch.object(
                form_review_providers, "analyze_workday_form_html",
                wraps=form_review_providers.analyze_workday_form_html) as analyzer:
            report = analyze_application_form("workday", html, route_url=self.workday_route)

        analyzer.assert_called_once_with(html)
        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["reason"], "WORKDAY_DYNAMIC_SEMANTICS_UNOBSERVED")

    def test_workday_projects_visible_metadata_and_never_returns_control_values(self):
        html = (
            '<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email" required value="PRIVATE_INPUT">'
            '<label for="country">Country</label><select id="country" name="country">'
            '<option value="private-option" selected>PRIVATE_SELECTED</option></select>'
            '<label for="note">Additional details</label>'
            '<textarea id="note" name="note">PRIVATE_ANSWER</textarea></form>'
        )

        report = analyze_application_form("workday", html, route_url=self.workday_route)

        self.assertEqual({key: report[key] for key in ("status", "reason", "fields")}, {
            "status": "REVIEW_REQUIRED", "reason": "WORKDAY_DYNAMIC_SEMANTICS_UNOBSERVED",
            "fields": [
                {"id": "email", "name": "email", "type": "email", "required": True,
                 "review_reason": "UNKNOWN_QUESTION"},
                {"id": "country", "name": "country", "type": "select", "required": False,
                 "review_reason": "UNKNOWN_QUESTION"},
                {"id": "note", "name": "note", "type": "textarea", "required": False,
                 "review_reason": "UNKNOWN_QUESTION"},
            ],
        })
        self.assertEqual(set(report), {"status", "reason", "fields", "dynamic_review"})
        self.assertEqual(validate_workday_dynamic_review(report["dynamic_review"]),
                         report["dynamic_review"])
        serialized = json.dumps(report)
        for secret in ("PRIVATE_INPUT", "private-option", "PRIVATE_SELECTED", "PRIVATE_ANSWER",
                       "Email address", "Additional details"):
            self.assertNotIn(secret, serialized)

    def test_workday_conditional_repeated_sensitive_and_unknown_semantics_fail_closed(self):
        html = (
            '<form><label for="country">Country</label>'
            '<select id="country" name="country" data-workday-conditional="false" '
            'data-workday-repeatable="false"></select>'
            '<label for="region">Region</label><input id="region" name="region" '
            'data-workday-conditional="true" data-workday-conditional-on="country" '
            'data-workday-repeatable="false">'
            '<label for="employer">Employer</label><input id="employer" name="employer" '
            'data-workday-conditional="false" data-workday-repeatable="true" '
            'data-workday-repeatable-group="experience">'
            '<label for="ssn">Social Security number</label><input id="ssn" name="ssn">'
            '<label for="custom">Custom item</label><input id="custom" name="custom" '
            'type="workday-widget"></form>'
        )

        report = analyze_application_form("workday", html, route_url=self.workday_route)

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        reasons = {field["id"]: field["review_reason"] for field in report["fields"]}
        self.assertEqual(reasons["region"], "UNKNOWN_QUESTION")
        self.assertEqual(reasons["employer"], "UNKNOWN_QUESTION")
        self.assertEqual(reasons["ssn"], "SENSITIVE_FIELD")
        self.assertEqual(reasons["custom"], "UNSUPPORTED_FIELD_TYPE")
        self.assertNotIn("Social Security number", json.dumps(report))

    def test_workday_unobserved_dynamic_semantics_and_malformed_markup_review(self):
        no_dynamic_evidence = analyze_application_form(
            "workday", '<form><input id="email" name="email" type="email"></form>',
            route_url=self.workday_route,
        )
        malformed = analyze_application_form("workday", "<form><input", route_url=self.workday_route)
        missing = analyze_application_form("workday", "<div>not a form</div>",
                                           route_url=self.workday_route)
        self.assertEqual(no_dynamic_evidence["fields"][0]["review_reason"], "UNKNOWN_QUESTION")
        self.assertEqual(malformed["status"], "REVIEW_REQUIRED")
        self.assertEqual(missing["reason"], "NO_FORM")
        self.assertEqual(missing["fields"], [])

    def test_lever_requiredness_is_known_only_when_static_markup_states_it(self):
        report = analyze_application_form(
            "lever",
            '<form><input id="email" name="email" type="email" required>'
            '<input id="phone" name="phone" type="tel" aria-required="true">'
            '<input id="first-name" name="first_name" aria-required="false">'
            '<input id="website" name="website"></form>',
            route_url=self.lever_route,
        )

        self.assertEqual(report["status"], "ANALYZED")
        self.assertEqual([field["required"] for field in report["fields"]],
                         [True, True, False, False])

    def test_unknown_custom_lever_prompts_require_review_and_do_not_leak_copy_or_values(self):
        markup = (
            '<form><input id="email" name="email" type="email">'
            '<label for="custom_q_982">What should we know about you?</label>'
            '<textarea id="custom_q_982" name="custom_q_982">PRIVATE_ANSWER</textarea></form>'
        )

        report = analyze_application_form("lever", markup, route_url=self.lever_route)

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertIn("UNKNOWN_QUESTION", [field["review_reason"] for field in report["fields"]])
        self.assertNotIn("What should we know about you?", json.dumps(report))
        self.assertNotIn("PRIVATE_ANSWER", json.dumps(report))

    def test_unrecognized_select_question_requires_review(self):
        report = analyze_application_form(
            "lever",
            '<form><label for="q">Choose your preferred arrangement</label>'
            '<select id="q" name="custom_arrangement"><option value="x">Private option</option></select></form>',
            route_url=self.lever_route,
        )

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["fields"][0]["review_reason"], "UNKNOWN_QUESTION")
        self.assertNotIn("Private option", json.dumps(report))

    def test_visible_custom_prompt_overrides_a_familiar_identifier(self):
        report = analyze_application_form(
            "lever",
            '<form><label for="email">Describe your availability</label>'
            '<input id="email" name="email" type="email"></form>',
            route_url=self.lever_route,
        )

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["fields"][0]["review_reason"], "UNKNOWN_QUESTION")
        self.assertNotIn("Describe your availability", json.dumps(report))

    def test_lever_challenge_and_non_native_interactive_widgets_fail_closed(self):
        challenge = analyze_application_form(
            "lever", '<form><div class="cf-turnstile">PRIVATE_CHALLENGE</div>'
            '<input id="email" name="email" type="email"></form>',
            route_url=self.lever_route,
        )
        widget = analyze_application_form(
            "lever", '<form><input id="email" name="email" type="email">'
            '<div role="combobox" aria-label="Work preference">Private options</div></form>',
            route_url=self.lever_route,
        )

        self.assertEqual(challenge["status"], "REVIEW_REQUIRED")
        self.assertEqual(challenge["reason"], "CHALLENGE_PROTECTED")
        self.assertEqual(widget["status"], "REVIEW_REQUIRED")
        self.assertEqual(widget["reason"], "UNSUPPORTED_INTERACTIVE_CONTROL")
        self.assertNotIn("PRIVATE_CHALLENGE", json.dumps(challenge))
        self.assertNotIn("Private options", json.dumps(widget))

    def test_lever_requires_one_unambiguous_form_boundary(self):
        no_form = analyze_application_form(
            "lever", '<input id="email" name="email" type="email">',
            route_url=self.lever_route,
        )
        multiple_forms = analyze_application_form(
            "lever", '<form><input id="email" name="email" type="email"></form>'
            '<form><input id="newsletter" name="email" type="email"></form>',
            route_url=self.lever_route,
        )
        outside_control = analyze_application_form(
            "lever", '<form><input id="email" name="email" type="email"></form>'
            '<input id="outside" name="newsletter_email" type="email">',
            route_url=self.lever_route,
        )

        self.assertEqual(no_form["status"], "REVIEW_REQUIRED")
        self.assertEqual(no_form["reason"], "NO_FORM")
        self.assertEqual(multiple_forms["status"], "REVIEW_REQUIRED")
        self.assertEqual(multiple_forms["reason"], "AMBIGUOUS_FORM")
        self.assertEqual(outside_control["status"], "REVIEW_REQUIRED")
        self.assertEqual(outside_control["reason"], "AMBIGUOUS_FORM")
        self.assertEqual(no_form["fields"], [])
        self.assertEqual(multiple_forms["fields"], [])
        self.assertEqual(outside_control["fields"], [])

    def test_sensitive_fields_require_human_review_for_both_providers(self):
        greenhouse = analyze_application_form(
            "greenhouse", '<form><input id="ssn" name="ssn"></form>',
            route_url=self.greenhouse_route,
        )
        lever = analyze_application_form(
            "lever", '<form><label for="birth">Date of birth</label>'
            '<input id="birth" name="birth_date"></form>',
            route_url=self.lever_route,
        )

        self.assertEqual(greenhouse["status"], "REVIEW_REQUIRED")
        self.assertEqual(greenhouse["fields"][0]["review_reason"], "SENSITIVE_FIELD")
        self.assertEqual(lever["status"], "REVIEW_REQUIRED")
        self.assertEqual(lever["reason"], "FORM_REQUIRES_HUMAN_REVIEW")
        self.assertEqual(lever["fields"][0]["review_reason"], "SENSITIVE_FIELD")
        self.assertNotIn("Date of birth", json.dumps(lever))

    def test_ambiguous_lever_controls_require_human_review(self):
        report = analyze_application_form(
            "lever", '<form><input id="same" name="email">'
            '<input id="same" name="other"></form>',
            route_url=self.lever_route,
        )

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["reason"], "AMBIGUOUS_CONTROL_IDENTITY")
        self.assertEqual([field["review_reason"] for field in report["fields"]],
                         ["AMBIGUOUS_FIELD", "AMBIGUOUS_FIELD"])

    def test_unsupported_providers_and_routes_return_fixed_codes(self):
        self.assertEqual(analyze_application_form(
            "unknown", "<form></form>", route_url=self.greenhouse_route,
        ), {"status": "REVIEW_REQUIRED", "reason": "PROVIDER_UNSUPPORTED", "fields": []})
        self.assertEqual(analyze_application_form(
            "lever", "<form></form>", route_url="https://jobs.lever.co.attacker.test/a/b",
        ), {"status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": []})
        self.assertIsNone(normalize_application_route("ashby", self.lever_route))

    def test_route_normalization_rejects_credentials_ports_and_unknown_paths(self):
        invalid_routes = (
            ("greenhouse", "https://user@boards.greenhouse.io/example/jobs/1234"),
            ("greenhouse", "https://boards.greenhouse.io:8443/example/jobs/1234"),
            ("lever", "https://jobs.lever.co:443/example/123e4567-e89b-12d3-a456-426614174000"),
            ("lever", "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000/unknown"),
        )
        for provider, route_url in invalid_routes:
            with self.subTest(provider=provider, route_url=route_url):
                self.assertIsNone(normalize_application_route(provider, route_url))


if __name__ == "__main__":
    unittest.main()
