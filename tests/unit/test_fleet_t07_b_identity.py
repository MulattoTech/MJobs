import unittest

from services.applications.adapters.bamboohr import BambooHRApplicationAdapter


class FleetT07BambooIdentityTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BambooHRApplicationAdapter()

    def test_rejects_raw_unicode_host_characters_that_casefold_to_ascii(self):
        for url in (
            "https://ſ.bamboohr.com/careers/123",
            "https://K.bamboohr.com/careers/123",
        ):
            with self.subTest(url=ascii(url)):
                self.assertIsNone(self.adapter.normalize_route({
                    "provider": "bamboohr", "url": url,
                }))

    def test_rejects_unicode_supplied_tenant_identity_aliases(self):
        for ascii_tenant, alias in (("s", "ſ"), ("k", "K")):
            with self.subTest(ascii_tenant=ascii_tenant, alias=ascii(alias)):
                self.assertIsNone(self.adapter.normalize_route({
                    "provider": "bamboohr",
                    "url": f"https://{ascii_tenant}.bamboohr.com/careers/123",
                    "tenant": alias,
                }))

    def test_preserves_ascii_case_normalization_tracking_and_idempotence(self):
        route = {
            "provider": "BAMBOOHR",
            "url": "https://AcMe.BambooHR.com/careers/12345?source=fixture",
            "tenant": "ACME",
            "requisition": "12345",
        }
        expected = {
            "provider": "bamboohr",
            "tenant": "acme",
            "requisition": "12345",
            "url": "https://acme.bamboohr.com/careers/12345",
        }
        normalized = self.adapter.normalize_route(route)
        self.assertEqual(normalized, expected)
        self.assertEqual(self.adapter.normalize_route(normalized), expected)

    def test_route_only_capability_boundary_is_unchanged(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": False,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })


if __name__ == "__main__":
    unittest.main()
