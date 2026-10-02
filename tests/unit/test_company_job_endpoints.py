"""Discovery tests for first-party, machine-readable employer endpoints."""
import unittest

from services.company_discovery.endpoints import (
    machine_readable_job_endpoints,
    parse_public_job_feed,
)


class CompanyJobEndpointTests(unittest.TestCase):
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
