"""Synthetic end-to-end coverage for sitemap-backed company career discovery."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx

from packages.persistence.store import Store


class SitemapCompanyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'sitemap.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _client(self, origin, pages):
        class FixtureTransport:
            bounded_redirects = True
            fixture_only = True

            def __init__(self):
                self.urls = []

            def allowed(self, url):
                parts = urlsplit(url)
                return (parts.scheme, parts.netloc) == ("https", urlsplit(origin).netloc)

            def get(self, url):
                self.urls.append(url)
                if url not in pages:
                    request = httpx.Request("GET", url)
                    response = httpx.Response(404, request=request)
                    raise httpx.HTTPStatusError("not found", request=request, response=response)
                return pages[url]

        return FixtureTransport()

    def test_sitemap_career_page_is_fetched_and_corroborated(self):
        from services.company_discovery.flywheel import discover_company_source

        origin = "https://employer.example"
        sitemap = origin + "/sitemap.xml"
        careers_sitemap = origin + "/sitemaps/careers.xml"
        careers = origin + "/careers"
        pages = {
            origin + "/": SimpleNamespace(
                url=origin + "/",
                text='<link rel="sitemap" type="application/xml" href="/sitemap.xml">',
            ),
            sitemap: SimpleNamespace(url=sitemap, text="<sitemapindex />"),
            careers_sitemap: SimpleNamespace(url=careers_sitemap, text="<urlset />"),
            careers: SimpleNamespace(
                url=careers,
                text='<a data-provider="mock-ats" data-tenant="employer" href="/ats/jobs">Open roles</a>',
            ),
            origin + "/ats/jobs": SimpleNamespace(url=origin + "/ats/jobs", json=lambda: {"jobs": []}),
        }
        client = self._client(origin, pages)

        # The XML parser contract is owned by the parallel career_pages slice.
        with patch("services.company_discovery.flywheel.sitemap_locations", create=True,
                   return_value=[sitemap]), patch(
            "services.company_discovery.flywheel.parse_sitemap", create=True,
            side_effect=[
                {"career_pages": [], "child_sitemaps": [careers_sitemap]},
                {"career_pages": [careers], "child_sitemaps": []},
            ],
        ):
            result = discover_company_source(self.store, {
                "company_name": "Employer Example", "company_website": origin + "/",
            }, client)

        self.assertEqual(result["careers_url"], careers)
        self.assertEqual(result["provider"], "mock-ats")
        self.assertIn(sitemap, client.urls)
        self.assertIn(careers_sitemap, client.urls)
        self.assertIn(careers, client.urls)

    def test_existing_common_route_fallback_runs_when_sitemap_has_no_career_page(self):
        from services.company_discovery.flywheel import discover_company_source

        origin = "https://employer.example"
        sitemap = origin + "/sitemap.xml"
        careers = origin + "/careers"
        pages = {
            origin + "/": SimpleNamespace(url=origin + "/", text="<main>Employer Example</main>"),
            sitemap: SimpleNamespace(url=sitemap, text="<urlset />"),
            careers: SimpleNamespace(
                url=careers,
                text='<a data-provider="mock-ats" data-tenant="employer" href="/ats/jobs">Open roles</a>',
            ),
            origin + "/ats/jobs": SimpleNamespace(url=origin + "/ats/jobs", json=lambda: {"jobs": []}),
        }
        client = self._client(origin, pages)

        with patch("services.company_discovery.flywheel.sitemap_locations", create=True,
                   return_value=[sitemap]), patch(
            "services.company_discovery.flywheel.parse_sitemap", create=True,
            return_value={"career_pages": [], "child_sitemaps": []},
        ):
            result = discover_company_source(self.store, {
                "company_name": "Employer Example", "company_website": origin + "/",
            }, client)

        self.assertEqual(result["careers_url"], careers)
        self.assertIn(careers, client.urls)

    def test_external_sitemap_locations_never_receive_a_request(self):
        from services.company_discovery.flywheel import discover_company_source

        origin = "https://employer.example"
        sitemap = origin + "/sitemap.xml"
        pages = {
            origin + "/": SimpleNamespace(url=origin + "/", text="<main>Employer Example</main>"),
            sitemap: SimpleNamespace(url=sitemap, text="<urlset />"),
        }
        client = self._client(origin, pages)

        with patch("services.company_discovery.flywheel.sitemap_locations", create=True,
                   return_value=[sitemap]), patch(
            "services.company_discovery.flywheel.parse_sitemap", create=True,
            return_value={"career_pages": ["https://outside.example/careers"], "child_sitemaps": []},
        ):
            with self.assertRaisesRegex(ValueError, "COMPANY_CAREERS_LINK_UNPROVEN"):
                discover_company_source(self.store, {
                    "company_name": "Employer Example", "company_website": origin + "/",
                }, client)

        self.assertEqual(client.urls, [origin + "/", sitemap, origin + "/careers",
                                       origin + "/jobs", origin + "/careers/jobs",
                                       origin + "/jobs/search", origin + "/work-with-us",
                                       origin + "/join-us", origin + "/join-our-team",
                                       origin + "/employment", origin + "/opportunities"])
        self.assertFalse(any(urlsplit(url).netloc != urlsplit(origin).netloc for url in client.urls))


if __name__ == "__main__":
    unittest.main()
