import json
import unittest
from unittest.mock import patch

from services.applications.adapters.teamtailor import TeamtailorApplicationAdapter


class TeamtailorApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = TeamtailorApplicationAdapter()

    def test_declares_only_route_and_value_free_review_support(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_normalizes_teamtailor_job_route_and_discards_tracking(self):
        result = self.adapter.normalize_route({
            "provider": "TEAMTAILOR",
            "url": "https://Acme.teamtailor.com/jobs/senior-engineer?source=fixture",
        })
        self.assertEqual(result, {
            "provider": "teamtailor",
            "tenant": "acme",
            "requisition": "senior-engineer",
            "url": "https://acme.teamtailor.com/jobs/senior-engineer",
        })

    def test_rejects_untrusted_hosts_and_route_shapes(self):
        invalid = (
            None,
            {"provider": "greenhouse", "url": "https://acme.teamtailor.com/jobs/role"},
            {"provider": "teamtailor", "url": "http://acme.teamtailor.com/jobs/role"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com.attacker.test/jobs/role"},
            {"provider": "teamtailor", "url": "https://user@acme.teamtailor.com/jobs/role"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com:444/jobs/role"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com/jobs/role/extra"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com/jobs/%2fadmin"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com/jobs/role#apply"},
            {"provider": "teamtailor", "url": "https://teamtailor.com/jobs/role"},
            {"provider": "teamtailor", "url": "https://acme.teamtailor.com/jobs/role/"},
        )
        for route in invalid:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_rejects_conflicting_route_metadata(self):
        url = "https://acme.teamtailor.com/jobs/senior-engineer"
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "teamtailor", "url": url, "tenant": "other",
        }))
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "teamtailor", "url": url, "requisition": "other",
        }))

    def test_analyze_page_uses_shared_value_free_form_analyzer(self):
        class Locator:
            def count(self):
                return 0

        class Page:
            def locator(self, selector):
                return Locator()

        page = Page()
        expected = {"status": "PASS", "reason": None, "fields": []}
        with patch("services.applications.adapters.teamtailor.analyze_form_page",
                   return_value=expected) as analyzer:
            self.assertEqual(self.adapter.analyze_page(page), expected)
        analyzer.assert_called_once_with(page)

    def test_challenge_protected_form_fails_closed_without_returning_markup(self):
        class Locator:
            def __init__(self, count):
                self._count = count

            def count(self):
                return self._count

        class Page:
            def locator(self, selector):
                return Locator(1 if "captcha" in selector else 0)

        result = self.adapter.analyze_page(Page())
        self.assertEqual(result, {
            "status": "UNKNOWN", "reason": "CHALLENGE_PROTECTED", "fields": [],
        })
        self.assertNotIn("PRIVATE_CHALLENGE", json.dumps(result))

    def test_unknown_sensitive_ambiguous_and_empty_forms_require_review(self):
        unsafe = (
            None,
            {"status": "UNKNOWN", "fields": []},
            {"status": "PASS", "fields": []},
            {"status": "PASS", "fields": [{
                "ambiguous": True, "sensitivity": "STANDARD", "name": "email",
            }]},
            {"status": "PASS", "fields": [{
                "ambiguous": False, "sensitivity": "SENSITIVE", "name": "visa_status",
            }]},
            {"status": "PASS", "fields": [{
                "ambiguous": False, "sensitivity": "STANDARD", "name": "custom_question",
            }]},
        )
        for analysis in unsafe:
            with self.subTest(analysis=analysis):
                self.assertTrue(self.adapter.requires_human_review(analysis))

    def test_known_standard_contact_metadata_can_be_reviewed_without_exposing_values(self):
        analysis = {"status": "PASS", "fields": [{
            "ambiguous": False, "sensitivity": "STANDARD", "tag": "input",
            "input_type": "email", "name": "email", "labels": [],
        }]}
        self.assertFalse(self.adapter.requires_human_review(analysis))
        report = json.dumps(analysis)
        self.assertNotIn("value", report)
        self.assertNotIn("PRIVATE", report)


if __name__ == "__main__":
    unittest.main()
