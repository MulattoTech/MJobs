"""Cross-provider contract checks for Team 7's adapter slice."""
import unittest

from services.applications.adapters.smartrecruiters import SmartRecruitersApplicationAdapter
from services.applications.adapters.teamtailor import TeamtailorApplicationAdapter
from services.applications.adapters.workable import WorkableApplicationAdapter


class FleetT07ProviderContractTests(unittest.TestCase):
    def test_adapters_publish_the_same_capability_contract(self):
        adapters = (
            SmartRecruitersApplicationAdapter(),
            TeamtailorApplicationAdapter(),
            WorkableApplicationAdapter(),
        )
        expected = {
            "route_normalization", "form_metadata_extraction",
            "human_review_for_uncertainty", "mock_fixture_upload",
            "document_upload", "live_form_interaction", "submission",
        }

        for adapter in adapters:
            with self.subTest(provider=adapter.provider):
                capabilities = adapter.capabilities()
                self.assertEqual(set(capabilities), expected)
                self.assertTrue(all(type(value) is bool for value in capabilities.values()))

    def test_adapters_normalize_to_the_same_route_identity_shape(self):
        cases = (
            (SmartRecruitersApplicationAdapter(), {
                "provider": "smartrecruiters",
                "url": "https://jobs.smartrecruiters.com/Acme/743999123456789-role?src=test",
            }),
            (TeamtailorApplicationAdapter(), {
                "provider": "teamtailor",
                "url": "https://Acme.teamtailor.com/jobs/role?src=test",
            }),
            (WorkableApplicationAdapter(), {
                "provider": "workable",
                "url": "https://apply.workable.com/Acme/j/role123?src=test",
            }),
        )
        expected = {"provider", "tenant", "requisition", "url"}

        for adapter, route in cases:
            with self.subTest(provider=adapter.provider):
                normalized = adapter.normalize_route(route)
                self.assertIsNotNone(normalized)
                self.assertEqual(set(normalized), expected)
                self.assertEqual(normalized["provider"], adapter.provider)
                self.assertEqual(normalized["url"].split("?", 1)[0], normalized["url"])


if __name__ == "__main__":
    unittest.main()
