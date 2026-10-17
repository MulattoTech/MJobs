import unittest

from services.applications.adapters.generic_html import GenericHTMLApplicationAdapter
from services.applications.adapters.recruitee import RecruiteeApplicationAdapter


class FleetT07ProviderContractTests(unittest.TestCase):
    def test_recruitee_rejects_url_whitespace_and_control_characters(self):
        adapter = RecruiteeApplicationAdapter()
        for url in (
            " https://example.recruitee.com/o/job",
            "https://example.recruitee.com/o/job\n?source=fixture",
            "https://example.recruitee.com/o/job\t?source=fixture",
        ):
            with self.subTest(url=repr(url)):
                self.assertIsNone(adapter.normalize_route({
                    "provider": "recruitee", "url": url,
                }))

    def test_generic_html_provider_identity_is_case_insensitive(self):
        self.assertEqual(GenericHTMLApplicationAdapter().normalize_route({
            "provider": "Generic_HTML", "url": "https://careers.example/jobs/42",
        }), {
            "provider": "generic_html", "tenant": "careers.example",
            "url": "https://careers.example/jobs/42",
        })


if __name__ == "__main__":
    unittest.main()
