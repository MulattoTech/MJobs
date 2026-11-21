"""Offline contract tests for the review-only iCIMS adapter."""
import unittest

from services.applications.adapters.icims import ICIMSApplicationAdapter


class ICIMSApplicationAdapterTests(unittest.TestCase):
    route = "https://careers-example.icims.com/jobs/6534/hr-it--project-manager/job"

    def setUp(self):
        self.adapter = ICIMSApplicationAdapter()

    def test_declares_only_route_and_review_capabilities(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_normalizes_canonical_job_and_application_mode_routes(self):
        normalized = self.adapter.normalize_route({
            "provider": "icims",
            "url": self.route + "?mode=apply&source=fixture",
            "tenant": "example",
            "requisition": "6534",
        })
        self.assertEqual(normalized, {
            "provider": "icims", "tenant": "example", "requisition": "6534",
            "url": self.route,
        })

        mixed_case_slug = self.adapter.normalize_route({
            "provider": "icims",
            "url": "https://careers-example.icims.com/jobs/6534/Senior-HR-Specialist/job",
        })
        self.assertEqual(
            mixed_case_slug["url"],
            "https://careers-example.icims.com/jobs/6534/Senior-HR-Specialist/job",
        )

    def test_rejects_noncanonical_origins_paths_and_conflicting_identity(self):
        invalid = [
            "http://careers-example.icims.com/jobs/6534/hr-it--project-manager/job",
            "https://user@careers-example.icims.com/jobs/6534/hr-it--project-manager/job",
            "https://careers-example.icims.com:443/jobs/6534/hr-it--project-manager/job",
            "https://careers-example.icims.com/jobs/6534/hr-it--project-manager/job#apply",
            "https://careers-example.icims.com/jobs/6534/hr-it--project-manager/job/extra",
            "https://careers-example.icims.com/jobs/%2f6534/hr-it--project-manager/job",
            "https://careers-example.icims.com/jobs/6534/%2f/job",
            "https://careers-example.icims.com/jobs/6534/../job",
            "https://careers-example.icims.com/jobs/6534/%2e%2e/job",
            "https://careers-example.icims.com/jobs/6534/senior-hr-specialist/job/title",
            "https://careers-example.icims.com/jobs/6534/１２３/job",
            "https://careers-example.icims.com/jobs/6534/role-Kelvin/job",
            "https://careers-example.icims.com/jobs/6534/" + "a" * 161 + "/job",
            "https://careers-example.icims.com/jobs/6534/hr-it--project-manager/job?src=éxample",
            "https://careers-example.icims.com/jobs/6534/hr-it--project-manager/job?src=ok\ninvalid",
            "https://careers-example.icims.com.evil.test/jobs/6534/hr-it--project-manager/job",
            "https://careers-example.icims.com/jobs/6534/hr-it--project-manager/apply",
            "https://careers-example.icims.com/jobs/6534/hr-it--project-manager/job\\evil",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertIsNone(self.adapter.normalize_route({"provider": "icims", "url": url}))

        self.assertIsNone(self.adapter.normalize_route({
            "provider": "icims", "url": self.route, "tenant": "other",
        }))
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "icims", "url": self.route, "requisition": "54321",
        }))


if __name__ == "__main__":
    unittest.main()
