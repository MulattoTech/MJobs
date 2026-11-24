"""Reject declared company feeds before enrollment/import and preserve recovery."""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery import flywheel
from services.company_discovery.domains import record_company_domain
from services.company_discovery.public_client import (
    PublicCompanyClient,
    PublicFetchError,
)


class CompanyFeedXmlPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'feed.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
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

    @staticmethod
    def feed(root, slug, *, declared=False, encoding="utf-8"):
        title = "Synthetic " + slug + " Engineer"
        declaration = ""
        if declared:
            declaration = '<!DOCTYPE ' + root + ' [<!ENTITY role "' + title + '">]>'
            title = "&role;"
        link = "https://employer.example/jobs/" + slug
        if root == "rss":
            content = (
                "<rss><channel><item><guid>" + slug + "</guid><title>" + title
                + "</title><link>" + link + "</link>"
                "<description>Python systems role</description></item></channel></rss>"
            )
        else:
            content = (
                '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>' + slug
                + "</id><title>" + title + "</title><summary>Python systems role</summary>"
                '<link rel="alternate" type="text/html" href="' + link + '"/>'
                "</entry></feed>"
            )
        return ('<?xml version="1.0"?>' + declaration + content).encode(encoding).decode("latin-1")

    def discover(self, body):
        pages = {
            "/robots.txt": "User-agent: *\nAllow: /",
            "/": '<html>Employer Example<a rel="careers" href="/careers">Careers</a></html>',
            "/careers": (
                '<html>Employer Example<link rel="alternate" type="application/rss+xml"'
                ' href="/jobs.xml"></html>'
            ),
            "/jobs.xml": body,
            "/jobs/blocked": "<html>Engineer at Employer Example</html>",
            "/jobs/saved": "<html>Engineer at Employer Example</html>",
            "/jobs/recovered": "<html>Engineer at Employer Example</html>",
        }

        def fetch(client, url, deadline):
            request = httpx.Request("GET", url)
            self.fetched.append(request.url.path)
            return httpx.Response(200, text=pages[request.url.path], request=request)

        candidate = {
            "company_name": "Employer Example",
            "company_website": "https://employer.example/",
        }
        with (
            patch.object(PublicCompanyClient, "_request_once", fetch),
            closing(PublicCompanyClient(candidate["company_website"])) as client,
        ):
            return flywheel.discover_company_source(self.store, candidate, client)

    def reopened_jobs(self):
        reopened = Store(self.store.url)
        try:
            with reopened.engine.connect() as connection:
                return {
                    table.name: [dict(row) for row in connection.execute(
                        select(table).order_by(table.c.id)
                    ).mappings()]
                    for table in (m.jobs, m.routes, m.postings)
                }
        finally:
            reopened.engine.dispose()

    def source_count(self):
        with self.store.engine.connect() as connection:
            return len(connection.execute(select(m.company_sources.c.id)).all())

    def test_declared_rss_cannot_enroll_source_fetch_job_or_persist_rows(self):
        before = self.reopened_jobs()
        with self.assertRaisesRegex(PublicFetchError, "^COMPANY_ATS_REVIEW_REQUIRED$"):
            self.discover(self.feed("rss", "blocked", declared=True, encoding="utf-16-le"))
        self.assertIn("/jobs.xml", self.fetched)
        self.assertNotIn("/jobs/blocked", self.fetched)
        self.assertEqual(self.source_count(), 0)
        self.assertEqual(self.reopened_jobs(), before)

    def test_declared_atom_preserves_saved_rows_and_ordinary_feed_recovers(self):
        saved = self.discover(self.feed("feed", "saved"))
        self.assertEqual(saved["jobs_imported"], 1)
        self.assertEqual(self.store.count("canonical_jobs"), 1)
        before = self.reopened_jobs()

        with self.assertRaisesRegex(PublicFetchError, "^COMPANY_ATS_REVIEW_REQUIRED$"):
            self.discover(self.feed("feed", "blocked", declared=True, encoding="utf-16-be"))
        self.assertNotIn("/jobs/blocked", self.fetched)
        self.assertEqual(self.source_count(), 1)
        self.assertEqual(self.reopened_jobs(), before)

        recovered = self.discover(self.feed("feed", "recovered", encoding="utf-16-be"))
        self.assertEqual(recovered["jobs_imported"], 1)
        self.assertEqual(self.store.count("canonical_jobs"), 2)
        self.assertEqual(self.source_count(), 1)
        self.assertEqual(
            {row["url"] for row in self.reopened_jobs()[m.routes.name]},
            {"https://employer.example/jobs/saved", "https://employer.example/jobs/recovered"},
        )
