"""Discovery tests for first-party, machine-readable employer endpoints."""
import unittest

from services.company_discovery.endpoints import (
    machine_readable_job_endpoints,
    parse_public_job_feed,
)


class CompanyJobEndpointTests(unittest.TestCase):
    def test_same_endpoint_uses_stronger_json_declaration_regardless_of_dom_order(self):
        declarations = (
            '<link rel="alternate" type="application/rss+xml" href="/careers/jobs">',
            '<link rel="alternate" type="application/feed+json" href="/careers/jobs">',
        )
        expected = [{
            "url": "https://careers.employer.example/careers/jobs",
            "strategy": "PUBLIC_JSON",
        }]

        for html in ("\n".join(declarations), "\n".join(reversed(declarations))):
            with self.subTest(html=html):
                self.assertEqual(machine_readable_job_endpoints(
                    "https://careers.employer.example/careers", html
                ), expected)

    def test_rejects_unsafe_invalid_and_unsupported_links_while_deduplicating_stably(self):
        declarations = (
            '<link rel="alternate" type="application/rss+xml" href="/careers/z.xml">',
            '<link rel="alternate" type="application/feed+json" href="/careers/a.json">',
            '<link rel="alternate" type="application/feed+json" href="/careers/a.json">',
            '<link rel="alternate" type="application/atom+xml" href="https://other.example/jobs.xml">',
            '<link rel="alternate" type="application/json" href="https://user:secret@careers.employer.example/jobs.json">',
            '<link rel="alternate" type="text/html" href="/careers/browser-only">',
            '<link rel="alternate" type="application/json" href="https://[invalid">',
        )
        expected = [
            {"url": "https://careers.employer.example/careers/a.json", "strategy": "PUBLIC_JSON"},
            {"url": "https://careers.employer.example/careers/z.xml", "strategy": "RSS"},
        ]

        for html in ("\n".join(declarations), "\n".join(reversed(declarations))):
            with self.subTest(html=html):
                self.assertEqual(machine_readable_job_endpoints(
                    "https://careers.employer.example/careers", html
                ), expected)

    def test_finds_supported_feed_links_in_preference_order_and_rejects_cross_origin(self):
        html = '''
        <link rel="alternate" type="application/rss+xml" href="/careers/jobs.xml">
        <link rel="alternate" type="application/feed+json" href="/careers/feed.json">
        <link rel="alternate" type="application/atom+xml" href="https://feeds.other.example/jobs.xml">
        <link rel="stylesheet" type="application/json" href="/assets/theme.json">
        '''
        self.assertEqual(machine_readable_job_endpoints(
            "https://careers.employer.example/careers", html
        ), [
            {"url": "https://careers.employer.example/careers/feed.json", "strategy": "PUBLIC_JSON"},
            {"url": "https://careers.employer.example/careers/jobs.xml", "strategy": "RSS"},
        ])

    def test_parses_bounded_rss_atom_and_json_feed_jobs(self):
        rss = '<rss><channel><item><title>Admin</title><link>https://employer.example/job/1</link></item></channel></rss>'
        atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Engineer</title><link href="https://employer.example/job/2"/></entry></feed>'
        json_feed = '{"version":"https://jsonfeed.org/version/1.1","items":[{"title":"Analyst","url":"https://employer.example/job/3"}]}'
        self.assertEqual(parse_public_job_feed(rss, "RSS")[0]["title"], "Admin")
        self.assertEqual(parse_public_job_feed(atom, "RSS")[0]["link"], "https://employer.example/job/2")
        self.assertEqual(parse_public_job_feed(json_feed, "PUBLIC_JSON")[0]["title"], "Analyst")

    def test_atom_ignores_self_and_enclosure_links_before_selecting_job_alternate(self):
        links = (
            '<link rel="self" href="https://employer.example/feed/123"/>',
            '<link rel="alternate" type="text/html" href="https://employer.example/jobs/123"/>',
            '<link rel="enclosure" href="https://employer.example/files/123.pdf"/>',
        )
        for ordered in (links, tuple(reversed(links))):
            with self.subTest(ordered=ordered):
                atom = (
                    '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Engineer</title>'
                    + "".join(ordered)
                    + '</entry></feed>'
                )
                items = parse_public_job_feed(atom, "RSS")
                self.assertEqual(items[0]["link"], "https://employer.example/jobs/123")

    def test_atom_accepts_repeated_identical_alternates(self):
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Engineer</title>'
            '<link rel="alternate" href="https://employer.example/jobs/123"/>'
            '<link href="https://employer.example/jobs/123"/>'
            '<link rel="self" href="https://employer.example/feed/123"/>'
            '</entry></feed>'
        )
        self.assertEqual(parse_public_job_feed(atom, "RSS")[0]["link"],
                         "https://employer.example/jobs/123")

    def test_atom_rejects_multiple_distinct_job_alternates(self):
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Engineer</title>'
            '<link rel="alternate" href="https://employer.example/jobs/123"/>'
            '<link rel="alternate" href="https://other.example/jobs/999"/>'
            '</entry></feed>'
        )
        with self.assertRaisesRegex(ValueError, "^COMPANY_JOB_FEED_INVALID$"):
            parse_public_job_feed(atom, "RSS")

    def test_atom_rejects_non_job_links_without_an_alternate(self):
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Engineer</title>'
            '<link rel="self" href="https://employer.example/feed/123"/>'
            '<link rel="enclosure" href="https://employer.example/files/123.pdf"/>'
            '</entry></feed>'
        )
        with self.assertRaisesRegex(ValueError, "^COMPANY_JOB_FEED_INVALID$"):
            parse_public_job_feed(atom, "RSS")

    def test_atom_selects_browser_alternates_beside_unsupported_media(self):
        for media in (None, "text/html", "application/xhtml+xml", " Text/HTML; charset=UTF-8 "):
            media_attribute = "" if media is None else f' type="{media}"'
            links = (
                '<link rel="alternate" type="application/pdf" href="https://employer.example/files/123.pdf"/>',
                f'<link rel="alternate"{media_attribute} href="https://employer.example/jobs/123"/>',
            )
            for ordered in (links, tuple(reversed(links))):
                with self.subTest(media=media, ordered=ordered):
                    atom = (
                        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Engineer</title>'
                        + "".join(ordered)
                        + '</entry></feed>'
                    )
                    self.assertEqual(parse_public_job_feed(atom, "RSS")[0]["link"],
                                     "https://employer.example/jobs/123")

    def test_atom_rejects_alternates_with_only_non_page_media(self):
        for media in ("application/pdf", "application/atom+xml", "application/json",
                      "text/plain", "image/svg+xml"):
            with self.subTest(media=media):
                atom = (
                    '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Engineer</title>'
                    f'<link rel="alternate" type="{media}" href="https://employer.example/files/123"/>'
                    '<link rel="alternate" type="text/html" href=" "/>'
                    '</entry></feed>'
                )
                with self.assertRaisesRegex(ValueError, "^COMPANY_JOB_FEED_INVALID$"):
                    parse_public_job_feed(atom, "RSS")

    def test_atom_keeps_distinct_html_and_xhtml_destinations_ambiguous(self):
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Engineer</title>'
            '<link rel="alternate" type="text/html" href="https://employer.example/jobs/123"/>'
            '<link rel="alternate" type="application/xhtml+xml" href="https://employer.example/jobs/456"/>'
            '</entry></feed>'
        )
        with self.assertRaisesRegex(ValueError, "^COMPANY_JOB_FEED_INVALID$"):
            parse_public_job_feed(atom, "RSS")

    def test_rejects_entity_expansion_and_non_job_items(self):
        with self.assertRaisesRegex(ValueError, "COMPANY_JOB_FEED_INVALID"):
            parse_public_job_feed('<!DOCTYPE rss [<!ENTITY x "large">]><rss/>', "RSS")
        with self.assertRaisesRegex(ValueError, "COMPANY_JOB_FEED_INVALID"):
            parse_public_job_feed(
                '{"version":"https://jsonfeed.org/version/1","items":[{"content_text":"not a job"}]}',
                "PUBLIC_JSON",
            )


if __name__ == "__main__":
    unittest.main()
