"""Negative and recovery acceptance for bounded sitemap discovery."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit

from packages.persistence.store import Store
from services.company_discovery.flywheel import discover_company_source
from services.company_discovery.public_client import HTTPStatusError
from tests.integration.test_spine import PROFILE


ORIGIN = "https://employer.example"
OFF_ORIGIN = "https://outside.example"
CAREERS_URL = ORIGIN + "/careers"
FEED_URL = ORIGIN + "/careers/jobs.xml"
FEED_XML = (
    "<rss version='2.0'><channel><item><guid>safe-job-1</guid>"
    "<title>Network Administrator</title><link>/careers/job-1</link>"
    "</item></channel></rss>"
)


class SitemapFixtureTransport:
    """Synthetic one-origin transport that records every attempted URL/method."""

    bounded_redirects = True
    fixture_only = True

    def __init__(self, pages, denied=()):
        self.pages = pages
        self.denied = set(denied)
        self.requests = []

    def allowed(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme, parsed.netloc) == ("https", "employer.example")

    def get(self, url):
        self.requests.append(("GET", url))
        if url in self.denied:
            raise HTTPStatusError(403, final_url=url)
        try:
            return self.pages[url]
        except KeyError as error:
            raise HTTPStatusError(404, final_url=url) from error


class CompanyDiscoverySitemapSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database_path = Path(self.temp.name) / "sitemap-safety.db"
        self.store = Store(f"sqlite:///{database_path.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _page(self, url, text):
        return SimpleNamespace(url=url, text=text)

    def _homepage(self, sitemap_hint=""):
        return self._page(ORIGIN + "/", f"<h1>Employer Example</h1>{sitemap_hint}")

    def _career_feed_pages(self):
        return {
            CAREERS_URL: self._page(
                CAREERS_URL,
                '<h1>Employer Example Careers</h1>'
                '<link rel="alternate" type="application/rss+xml" href="/careers/jobs.xml">',
            ),
            FEED_URL: self._page(FEED_URL, FEED_XML),
        }

    def _discover(self, client):
        return discover_company_source(self.store, {
            "company_name": "Employer Example",
            "company_website": ORIGIN + "/",
        }, client)

    def _source_count(self):
        return self.store.count("company_job_sources")

    def _assert_get_only(self, client):
        self.assertTrue(client.requests)
        self.assertEqual({method for method, _ in client.requests}, {"GET"})
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_malformed_sitemap_does_not_block_safe_guessed_route_recovery(self):
        pages = {
            ORIGIN + "/": self._homepage(),
            ORIGIN + "/sitemap.xml": self._page(ORIGIN + "/sitemap.xml", "<urlset><url><loc>"),
            **self._career_feed_pages(),
        }
        client = SitemapFixtureTransport(pages)

        result = self._discover(client)

        self.assertEqual(result["endpoint"], FEED_URL)
        self.assertEqual(result["jobs_imported"], 1)
        self.assertIn(("GET", ORIGIN + "/sitemap.xml"), client.requests)
        self.assertIn(("GET", CAREERS_URL), client.requests)
        self.assertTrue(all(urlsplit(url).netloc == "employer.example" for _, url in client.requests))
        self._assert_get_only(client)

    def test_irrelevant_sitemap_entries_fall_back_without_enrolling_sitemap_target(self):
        pages = {
            ORIGIN + "/": self._homepage(),
            ORIGIN + "/sitemap.xml": self._page(
                ORIGIN + "/sitemap.xml",
                '<urlset><url><loc>https://employer.example/about</loc></url></urlset>',
            ),
            **self._career_feed_pages(),
        }
        client = SitemapFixtureTransport(pages)

        result = self._discover(client)

        self.assertEqual(result["careers_url"], CAREERS_URL)
        self.assertEqual(result["endpoint"], FEED_URL)
        self.assertIn(("GET", CAREERS_URL), client.requests)
        self.assertEqual(self._source_count(), 1)
        self._assert_get_only(client)

    def test_off_origin_sitemap_hint_is_never_requested_and_cannot_enroll_a_source(self):
        pages = {
            ORIGIN + "/": self._homepage(
                '<link rel="sitemap" href="https://outside.example/sitemap.xml">'
            ),
            ORIGIN + "/sitemap.xml": self._page(ORIGIN + "/sitemap.xml", "<urlset/>"),
        }
        client = SitemapFixtureTransport(pages)

        with self.assertRaisesRegex(ValueError, "COMPANY_CAREERS_LINK_UNPROVEN"):
            self._discover(client)

        urls = [url for _, url in client.requests]
        self.assertNotIn(OFF_ORIGIN + "/sitemap.xml", urls)
        self.assertIn(ORIGIN + "/sitemap.xml", urls)
        self.assertEqual(self._source_count(), 0)
        self.assertTrue(all(urlsplit(url).netloc == "employer.example" for url in urls))
        self._assert_get_only(client)

    def test_off_origin_sitemap_location_is_never_requested_or_enrolled(self):
        sitemap_url = ORIGIN + "/sitemap.xml"
        pages = {
            ORIGIN + "/": self._homepage(),
            sitemap_url: self._page(
                sitemap_url,
                f"<urlset><url><loc>{OFF_ORIGIN}/careers/jobs</loc></url></urlset>",
            ),
        }
        client = SitemapFixtureTransport(pages)

        with self.assertRaisesRegex(ValueError, "COMPANY_CAREERS_LINK_UNPROVEN"):
            self._discover(client)

        urls = [url for _, url in client.requests]
        self.assertIn(("GET", sitemap_url), client.requests)
        self.assertNotIn(OFF_ORIGIN + "/careers/jobs", urls)
        self.assertTrue(all(urlsplit(url).netloc == "employer.example" for url in urls))
        self.assertEqual(self._source_count(), 0)
        self._assert_get_only(client)

    def test_robots_denied_sitemap_fetch_returns_to_same_origin_route_fallback(self):
        sitemap_url = ORIGIN + "/sitemap.xml"
        pages = {ORIGIN + "/": self._homepage(), **self._career_feed_pages()}
        client = SitemapFixtureTransport(pages, denied={sitemap_url})

        result = self._discover(client)

        self.assertEqual(result["careers_url"], CAREERS_URL)
        self.assertEqual(result["endpoint"], FEED_URL)
        self.assertIn(("GET", sitemap_url), client.requests)
        self.assertIn(("GET", CAREERS_URL), client.requests)
        self.assertEqual(self._source_count(), 1)
        self.assertTrue(all(urlsplit(url).netloc == "employer.example" for _, url in client.requests))
        self._assert_get_only(client)

    def test_robots_denied_feed_fetch_does_not_enroll_or_submit(self):
        sitemap_url = ORIGIN + "/sitemap.xml"
        pages = {
            ORIGIN + "/": self._homepage(),
            sitemap_url: self._page(
                sitemap_url,
                f"<urlset><url><loc>{CAREERS_URL}</loc></url></urlset>",
            ),
            **self._career_feed_pages(),
        }
        client = SitemapFixtureTransport(pages, denied={FEED_URL})

        with self.assertRaises(HTTPStatusError) as failure:
            self._discover(client)

        self.assertEqual(failure.exception.status_code, 403)
        self.assertIn(("GET", FEED_URL), client.requests)
        self.assertEqual(self._source_count(), 0)
        self.assertEqual(self.store.jobs("fixture"), [])
        self.assertTrue(all(urlsplit(url).netloc == "employer.example" for _, url in client.requests))
        self._assert_get_only(client)


if __name__ == "__main__":
    unittest.main()
