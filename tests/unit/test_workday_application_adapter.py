"""Contract tests for Workday's local, review-first adapter boundary."""
import json
import unittest
from unittest.mock import patch

from services.applications.adapters.workday import WorkdayApplicationAdapter
from services.applications.form_review_providers import (
    analyze_application_form,
    normalize_application_route,
)


class WorkdayApplicationAdapterTests(unittest.TestCase):
    route_url = (
        "https://Acme.wd5.myworkdayjobs.com/en-US/AcmeCareers/job/"
        "San-Francisco/Software_Engineer_R12345?source=fixture"
    )

    def setUp(self):
        self.adapter = WorkdayApplicationAdapter()

    def test_normalizes_supported_tenant_locale_and_requisition_route(self):
        self.assertEqual(self.adapter.normalize_route({
            "provider": "workday", "url": self.route_url,
        }), {
            "provider": "workday",
            "tenant": "acme",
            "requisition": "R12345",
            "url": (
                "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers/job/"
                "San-Francisco/Software_Engineer_R12345"
            ),
        })

    def test_normalizes_common_workday_requisition_id_forms(self):
        for raw_id, normalized_id in (
                ("JR1997186", "JR1997186"), ("R-277676", "R-277676"),
                ("R026344-1", "R026344-1"), ("2015207", "2015207")):
            with self.subTest(requisition=raw_id):
                route = self.adapter.normalize_route({
                    "provider": "workday",
                    "url": (
                        "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/"
                        f"Title_{raw_id.lower()}"
                    ),
                })
                self.assertEqual(route["requisition"], normalized_id)

    def test_rejects_malformed_foreign_credentialed_and_port_confused_routes(self):
        invalid_urls = (
            "http://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Title_R12",
            "https://acme.wd5.myworkdayjobs.com.attacker.test/en-US/Acme/job/Title_R12",
            "https://attacker.test/acme.wd5.myworkdayjobs.com/en-US/Acme/job/Title_R12",
            "https://user@acme.wd5.myworkdayjobs.com/en-US/Acme/job/Title_R12",
            "https://user:pass@acme.wd5.myworkdayjobs.com/en-US/Acme/job/Title_R12",
            "https://acme.wd5.myworkdayjobs.com:443/en-US/Acme/job/Title_R12",
            "https://acme.wd5.myworkdayjobs.com:8443/en-US/Acme/job/Title_R12",
            "https://wd5.myworkdayjobs.com/en-US/Acme/job/Title_R12",
            "https://acme.wd5.otherworkdayjobs.com/en-US/Acme/job/Title_R12",
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Title",
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Title_R12/extra",
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Title%2FR12",
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Title_R12#section",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertIsNone(self.adapter.normalize_route({
                    "provider": "workday", "url": url,
                }))

    def test_rejects_conflicting_provider_tenant_or_requisition_metadata(self):
        base = {"provider": "workday", "url": self.route_url}
        self.assertIsNone(self.adapter.normalize_route(dict(base, provider="lever")))
        self.assertIsNone(self.adapter.normalize_route(dict(base, tenant="other")))
        self.assertIsNone(self.adapter.normalize_route(dict(base, requisition="R999")))

    def test_capabilities_declare_only_local_review_and_keep_public_actions_disabled(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": True,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_registry_dispatches_workday_to_value_free_form_review(self):
        html = (
            '<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email" required value="PRIVATE_VALUE">'
            '</form>'
        )
        report = analyze_application_form("WORKDAY", html, route_url=self.route_url)
        self.assertEqual(set(report), {"status", "reason", "fields", "dynamic_review"})
        self.assertEqual({key: report[key] for key in ("status", "reason", "fields")}, {
            "status": "REVIEW_REQUIRED", "reason": "WORKDAY_DYNAMIC_SEMANTICS_UNOBSERVED",
            "fields": [{"id": "email", "name": "email", "type": "email",
                        "required": True, "review_reason": "UNKNOWN_QUESTION"}],
        })
        from services.applications.workday_dynamic_review import validate_workday_dynamic_review
        envelope = validate_workday_dynamic_review(report["dynamic_review"])
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope, report["dynamic_review"])
        self.assertNotIn("PRIVATE_VALUE", json.dumps(report))
        self.assertNotIn("Email address", json.dumps(report))
        self.assertEqual(normalize_application_route("workday", self.route_url)["provider"],
                         "workday")

    def test_unknown_sensitive_and_challenge_forms_require_human_review(self):
        unknown = analyze_application_form(
            "workday",
            '<form><label for="question">What do you enjoy learning?</label>'
            '<textarea id="question" name="question">PRIVATE_ANSWER</textarea></form>',
            route_url=self.route_url,
        )
        sensitive = analyze_application_form(
            "workday", '<form><input id="ssn" name="ssn"></form>',
            route_url=self.route_url,
        )
        challenge = analyze_application_form(
            "workday", '<form><div class="cf-turnstile">PRIVATE_CHALLENGE</div>'
            '<input id="email" name="email" type="email"></form>',
            route_url=self.route_url,
        )
        for report in (unknown, sensitive, challenge):
            self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(unknown["fields"][0]["review_reason"], "UNKNOWN_QUESTION")
        self.assertEqual(sensitive["fields"][0]["review_reason"], "SENSITIVE_FIELD")
        self.assertEqual(challenge["reason"], "CHALLENGE_PROTECTED")
        for secret in ("PRIVATE_ANSWER", "PRIVATE_CHALLENGE", "What do you enjoy learning?"):
            self.assertNotIn(secret, json.dumps((unknown, challenge)))

    def test_changed_form_snapshot_is_not_accepted_for_review(self):
        original = {"status": "PASS", "fields": [{"id": "email"}]}
        changed = {"status": "PASS", "fields": [{"id": "phone"}]}
        with patch.object(self.adapter, "analyze_page", return_value=changed):
            result = self.adapter.validate_page(object(), original)
        self.assertEqual(result, {"status": "UNKNOWN", "reason": "FORM_CHANGED"})

    def test_unknown_provider_and_invalid_workday_route_fail_closed(self):
        unsupported = analyze_application_form("unknown", "<form></form>", route_url=self.route_url)
        invalid_route = analyze_application_form(
            "workday", "<form></form>", route_url="https://attacker.test/job/Title_R12",
        )
        self.assertEqual(unsupported["reason"], "PROVIDER_UNSUPPORTED")
        self.assertEqual(invalid_route["reason"], "ROUTE_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
