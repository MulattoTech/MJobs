import unittest

from services.company_discovery.search import (
    DisabledWebDiscoveryProvider,
    search_company_careers,
)


class _CustomProvider:
    provider_id = "test-custom-endpoint"

    def __init__(self):
        self.calls = []

    def search(self, query, *, limit):
        self.calls.append((query, limit))
        return [
            {"url": "https://careers.acme.example/jobs", "title": "Acme Careers", "snippet": "Open roles"},
            {"url": "javascript:alert(1)", "title": "Bad result", "snippet": ""},
            {"url": "https://user:secret@careers.acme.example", "title": "Bad credentials", "snippet": ""},
        ]


class CompanySearchTests(unittest.TestCase):
    def test_disabled_provider_is_default_and_returns_no_candidates(self):
        self.assertEqual(search_company_careers("Acme"), ())
        self.assertEqual(DisabledWebDiscoveryProvider().provider_id, "disabled")

    def test_injected_provider_is_neutral_and_results_remain_untrusted_candidates(self):
        provider = _CustomProvider()

        results = search_company_careers("Acme", provider, limit=3)

        self.assertEqual(provider.calls, [("Acme careers jobs", 3)])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://careers.acme.example/jobs")
        self.assertEqual(results[0].provider, "test-custom-endpoint")
        self.assertEqual(results[0].evidence, ("SEARCH_PROVIDER_RESULT",))

    def test_rejects_out_of_range_result_limits(self):
        with self.assertRaises(ValueError):
            search_company_careers("Acme", limit=0)


if __name__ == "__main__":
    unittest.main()
