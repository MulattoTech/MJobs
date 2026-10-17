"""Identity hardening across the owned T07 provider route adapters."""
import unittest

from services.applications.adapters.smartrecruiters import SmartRecruitersApplicationAdapter
from services.applications.adapters.teamtailor import TeamtailorApplicationAdapter
from services.applications.adapters.workable import WorkableApplicationAdapter


class FleetT07ProviderIdentityTests(unittest.TestCase):
    def test_ascii_case_variants_remain_canonical_and_idempotent(self):
        cases = (
            (SmartRecruitersApplicationAdapter(), {
                "provider": "SMARTRECRUITERS",
                "url": "https://JOBS.SMARTRECRUITERS.COM/ACMESOFT/743999123456789-Engineer?source=fixture",
            }),
            (TeamtailorApplicationAdapter(), {
                "provider": "TEAMTAILOR",
                "url": "https://ACME.teamtailor.com/jobs/Senior-Engineer?source=fixture",
            }),
            (WorkableApplicationAdapter(), {
                "provider": "WORKABLE",
                "url": "https://APPLY.WORKABLE.COM/ACMESOFT/j/Role123?source=fixture",
            }),
        )
        for adapter, route in cases:
            with self.subTest(provider=adapter.provider):
                normalized = adapter.normalize_route(route)
                self.assertIsNotNone(normalized)
                self.assertEqual(adapter.normalize_route(normalized), normalized)
                self.assertEqual(normalized["provider"], adapter.provider)

    def test_rejects_unicode_host_and_path_identity_aliases(self):
        cases = (
            (SmartRecruitersApplicationAdapter(), {
                "provider": "smartrecruiters",
                "url": "https://jobs.smartrecruiterſ.com/acmesoft/743999123456789-role",
            }),
            (SmartRecruitersApplicationAdapter(), {
                "provider": "smartrecruiters",
                "url": "https://jobs.smartrecruiters.com/acmeſoft/743999123456789-role",
            }),
            (TeamtailorApplicationAdapter(), {
                "provider": "teamtailor",
                "url": "https://Kcme.teamtailor.com/jobs/role",
            }),
            (TeamtailorApplicationAdapter(), {
                "provider": "teamtailor",
                "url": "https://acmesoft.teamtailor.com/jobs/roleſ",
            }),
            (WorkableApplicationAdapter(), {
                "provider": "workable",
                "url": "https://apply.workable.com/acmeſoft/j/role123",
            }),
            (WorkableApplicationAdapter(), {
                "provider": "workable",
                "url": "https://apply.workable.com/acmesoft/j/roleſ123",
            }),
        )
        for adapter, route in cases:
            with self.subTest(provider=adapter.provider, url=route["url"]):
                self.assertIsNone(adapter.normalize_route(route))

    def test_rejects_unicode_identity_metadata_and_provider_aliases(self):
        cases = (
            (SmartRecruitersApplicationAdapter(), {
                "provider": "smartrecruiterſ",
                "url": "https://jobs.smartrecruiters.com/acmesoft/743999123456789-role",
            }),
            (SmartRecruitersApplicationAdapter(), {
                "provider": "smartrecruiters", "tenant": "acmeſoft",
                "url": "https://jobs.smartrecruiters.com/acmesoft/743999123456789-role",
            }),
            (TeamtailorApplicationAdapter(), {
                "provider": "teamtailor", "tenant": "acmeſoft",
                "url": "https://acmesoft.teamtailor.com/jobs/role",
            }),
            (TeamtailorApplicationAdapter(), {
                "provider": "teamtailor", "requisition": "roleſ",
                "url": "https://acmesoft.teamtailor.com/jobs/roles",
            }),
            (WorkableApplicationAdapter(), {
                "provider": "workable", "tenant": "acmeſoft",
                "url": "https://apply.workable.com/acmesoft/j/role123",
            }),
            (WorkableApplicationAdapter(), {
                "provider": "workable", "requisition": "roleſ123",
                "url": "https://apply.workable.com/acmesoft/j/roles123",
            }),
        )
        for adapter, route in cases:
            with self.subTest(provider=adapter.provider, route=route):
                self.assertIsNone(adapter.normalize_route(route))

    def test_smartrecruiters_rejects_raw_whitespace_and_c0_controls(self):
        invalid_urls = (
            " https://jobs.smartrecruiters.com/acmesoft/743999123456789-role?source=fixture",
            "https://jobs.smartrecruiters.com/acmesoft/743999123456789-role?source=fixture\n",
            "https://jobs.smartrecruiters.com/acmesoft/743999123456789-role?source=fixture\x00",
        )
        adapter = SmartRecruitersApplicationAdapter()
        for url in invalid_urls:
            with self.subTest(url=repr(url)):
                self.assertIsNone(adapter.normalize_route({
                    "provider": "smartrecruiters", "url": url,
                }))


if __name__ == "__main__":
    unittest.main()
