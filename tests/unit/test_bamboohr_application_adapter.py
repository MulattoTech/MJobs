"""Contract tests for BambooHR's route-only, fail-closed adapter boundary."""
import unittest

from services.applications.adapters.bamboohr import BambooHRApplicationAdapter


class BambooHRApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BambooHRApplicationAdapter()

    def test_normalizes_public_bamboohr_careers_job_route(self):
        self.assertEqual(self.adapter.normalize_route({
            "provider": "bamboohr",
            "url": "https://Acme.bamboohr.com/careers/12345?source=fixture",
        }), {
            "provider": "bamboohr",
            "tenant": "acme",
            "requisition": "12345",
            "url": "https://acme.bamboohr.com/careers/12345",
        })

    def test_rejects_malformed_foreign_credentialed_and_ambiguous_routes(self):
        invalid_urls = (
            "http://acme.bamboohr.com/careers/12345",
            "https://acme.bamboohr.com.attacker.test/careers/12345",
            "https://attacker.test/acme.bamboohr.com/careers/12345",
            "https://user@acme.bamboohr.com/careers/12345",
            "https://user:pass@acme.bamboohr.com/careers/12345",
            "https://acme.bamboohr.com:443/careers/12345",
            "https://www.bamboohr.com/careers/12345",
            "https://acme.bamboohr.com/careers/12345/extra",
            "https://acme.bamboohr.com/careers/not-a-job-id",
            "https://acme.bamboohr.com/careers/12345/",
            "https://acme.bamboohr.com/careers%2F12345",
            "https://acme.bamboohr.com/careers/12345#apply",
            "https://acme.bamboohr.com/careers/12345?jobId=67890",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertIsNone(self.adapter.normalize_route({
                    "provider": "bamboohr", "url": url,
                }))

    def test_rejects_provider_or_route_metadata_conflicts(self):
        base = {
            "provider": "bamboohr",
            "url": "https://acme.bamboohr.com/careers/12345",
        }
        self.assertIsNone(self.adapter.normalize_route(dict(base, provider="lever")))
        self.assertIsNone(self.adapter.normalize_route(dict(base, tenant="other")))
        self.assertIsNone(self.adapter.normalize_route(dict(base, requisition="67890")))

    def test_declares_only_route_normalization_as_supported(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": False,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_all_form_semantics_fail_closed_without_echoing_supplied_answers(self):
        html = (
            '<form><label>What is your current salary?</label>'
            '<textarea>PRIVATE_ANSWER</textarea></form>'
        )
        report = self.adapter.analyze_form(html)
        self.assertEqual(report, {
            "status": "REVIEW_REQUIRED",
            "reason": "FORM_SEMANTICS_UNSUPPORTED",
        })
        self.assertNotIn("PRIVATE_ANSWER", repr(report))
        self.assertNotIn("current salary", repr(report))
        self.assertTrue(self.adapter.requires_human_review(report))


if __name__ == "__main__":
    unittest.main()
