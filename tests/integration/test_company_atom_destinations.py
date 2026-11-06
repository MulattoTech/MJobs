"""Atom job-page selection through the real, synthetic discovery/import path."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery import flywheel
from services.company_discovery.domains import record_company_domain
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError


class CompanyAtomDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'test.db').as_posix()}")
        self.store.migrate()
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "verified-employer", "name": "Employer Example", "domain": "",
            })
        record_company_domain(
            self.store, "verified-employer", "employer.example",
            "VERIFIED_EXISTING_DOMAIN", ["synthetic independent employer record"],
        )
        self.fetched = []

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _discover(self, entries):
        pages = {
            "/robots.txt": "User-agent: *\nAllow: /",
            "/": '<html>Employer Example<a rel="careers" href="/careers">Careers</a></html>',
            "/careers": (
                '<html>Employer Example<link rel="alternate" type="application/atom+xml"'
                ' href="/jobs.atom"></html>'
            ),
            "/jobs.atom": '<feed xmlns="http://www.w3.org/2005/Atom">' + entries + '</feed>',
            "/jobs/123": "<html>Engineer at Employer Example</html>",
            "/files/123.pdf": "synthetic attachment",
        }

        def fetch(client, url, deadline):
            request = httpx.Request("GET", url)
            self.fetched.append(request.url.path)
            return httpx.Response(200, text=pages[request.url.path], request=request)

        candidate = {
            "company_name": "Employer Example",
            "company_website": "https://employer.example/",
        }
        with patch.object(PublicCompanyClient, "_request_once", fetch):
            with closing(PublicCompanyClient(candidate["company_website"])) as client:
                return flywheel.discover_company_source(self.store, candidate, client)

    @staticmethod
    def _entry(links, identity="job-123"):
        return (
            f"<entry><id>{identity}</id><title>Engineer</title>"
            "<summary>Python systems role</summary>" + links + "</entry>"
        )

    def test_html_destination_is_imported_idempotently_without_fetching_pdf(self):
        links = (
            '<link rel="alternate" type="application/pdf" href="https://employer.example/files/123.pdf"/>',
            '<link rel="alternate" type="text/html" href="https://employer.example/jobs/123"/>',
        )
        for ordered in (links, tuple(reversed(links))):
            with self.subTest(ordered=ordered):
                result = self._discover(self._entry("".join(ordered)))
                self.assertEqual(result["jobs_imported"], 1)
                self.assertEqual(result["provider"], "rss")
                self.assertEqual(self.store.count("canonical_jobs"), 1)
                with self.store.engine.connect() as connection:
                    routes = connection.execute(select(m.routes.c.url)).scalars().all()
                    postings = connection.execute(select(m.postings.c.application_url)).scalars().all()
                self.assertEqual(routes, ["https://employer.example/jobs/123"])
                self.assertEqual(postings, ["https://employer.example/jobs/123"])
        self.assertIn("/jobs/123", self.fetched)
        self.assertNotIn("/files/123.pdf", self.fetched)

    def test_non_page_entry_prevents_source_enrollment_and_partial_job_import(self):
        rejected = self._entry(
            '<link rel="alternate" type="application/pdf" href="https://employer.example/files/123.pdf"/>',
            identity="attachment-only",
        )
        accepted = self._entry(
            '<link rel="alternate" type="text/html" href="https://employer.example/jobs/123"/>',
        )
        for entries in (rejected, accepted + rejected):
            with self.subTest(entries=entries):
                with self.assertRaisesRegex(PublicFetchError, "COMPANY_ATS_REVIEW_REQUIRED"):
                    self._discover(entries)
                self.assertEqual(self.store.count("canonical_jobs"), 0)
                with self.store.engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.routes.c.id)).all(), [])
                    self.assertEqual(connection.execute(select(m.company_sources.c.id)).all(), [])
        self.assertNotIn("/files/123.pdf", self.fetched)
        self.assertNotIn("/jobs/123", self.fetched)


if __name__ == "__main__":
    unittest.main()
