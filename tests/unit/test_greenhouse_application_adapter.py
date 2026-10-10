import unittest

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter


class GreenhouseApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = GreenhouseApplicationAdapter()

    def test_normalizes_supported_https_greenhouse_route(self):
        normalized = self.adapter.normalize_route({
            "provider": "greenhouse",
            "url": "https://job-boards.greenhouse.io/example/jobs/1234?utm_source=fixture",
        })

        self.assertEqual(normalized, {
            "provider": "greenhouse",
            "tenant": "example",
            "requisition": "1234",
            "url": "https://boards.greenhouse.io/example/jobs/1234",
        })

    def test_rejects_untrusted_route_shapes_and_provider_mismatch(self):
        invalid_routes = (
            {"provider": "lever", "url": "https://boards.greenhouse.io/example/jobs/1234"},
            {"provider": "greenhouse", "url": "http://boards.greenhouse.io/example/jobs/1234"},
            {"provider": "greenhouse", "url": "https://boards.greenhouse.io.attacker.test/example/jobs/1234"},
            {"provider": "greenhouse", "url": "https://boards.greenhouse.io/example/jobs/1234/extra"},
            {"provider": "greenhouse", "url": "https://boards.greenhouse.io/example/jobs/not-a-requisition"},
            {"provider": "greenhouse", "url": "https://user@boards.greenhouse.io/example/jobs/1234"},
            {"provider": "greenhouse", "url": "https://boards.greenhouse.io:8443/example/jobs/1234"},
        )

        for route in invalid_routes:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_uncertain_fields_require_human_review_and_submission_is_never_supported(self):
        analysis = {
            "status": "PASS",
            "fields": [
                {"sensitivity": "STANDARD", "ambiguous": False},
                {"sensitivity": "SENSITIVE", "ambiguous": False},
            ],
        }

        self.assertTrue(self.adapter.requires_human_review(analysis))
        self.assertFalse(self.adapter.capabilities()["submission"])
        self.assertFalse(self.adapter.capabilities()["live_form_interaction"])

    def test_standard_unambiguous_analysis_does_not_require_manual_review(self):
        analysis = {
            "status": "PASS",
            "fields": [{"sensitivity": "STANDARD", "ambiguous": False}],
        }

        self.assertFalse(self.adapter.requires_human_review(analysis))

    def test_unknown_or_malformed_analysis_requires_human_review(self):
        self.assertTrue(self.adapter.requires_human_review({"status": "UNKNOWN", "fields": []}))
        self.assertTrue(self.adapter.requires_human_review(None))


if __name__ == "__main__":
    unittest.main()
