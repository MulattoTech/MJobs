"""Synthetic WWR XML parser rejection and durable collection regressions."""
from pathlib import Path
import tempfile
import unittest

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.wwr import FEED_URL, SourceError, WeWorkRemotely
from tests.integration.test_spine import PROFILE


ENCODINGS = (
    ("utf-8", "utf-8"),
    ("utf-16", "utf-16"),
    ("utf-16", "utf-16-le"),
    ("utf-16", "utf-16-be"),
    ("iso-8859-1", "iso-8859-1"),
)


def rss(*, declaration="", title="Synthetic Employer: Engineer",
        description="Build Python systems.", slug="synthetic-engineer",
        encoding="utf-8", codec=None):
    return (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        f'{declaration}<rss version="2.0"><channel><item>'
        f'<title>{title}</title><description>{description}</description>'
        f'<guid>https://weworkremotely.com/remote-jobs/{slug}</guid>'
        f'<link>https://weworkremotely.com/remote-jobs/{slug}</link>'
        '</item></channel></rss>'
    ).encode(codec or encoding)


class ObservedStream(httpx.SyncByteStream):
    def __init__(self, payload):
        self.payload = payload
        self.closed = False

    def __iter__(self):
        for offset in range(0, len(self.payload), 31):
            yield self.payload[offset:offset + 31]

    def close(self):
        self.closed = True


class WeWorkRemotelyXmlParserTests(unittest.TestCase):
    def adapter(self, payload):
        visited = []
        stream = ObservedStream(payload)

        def respond(request):
            visited.append(str(request.url))
            return httpx.Response(200, stream=stream)

        return WeWorkRemotely(transport=httpx.MockTransport(respond)), visited, stream

    def test_rejects_dtds_in_all_supported_fixture_encodings(self):
        declarations = (
            ('<!DOCTYPE rss>', "Synthetic Employer: Engineer", "Python systems."),
            ('<!DOCTYPE rss [<!ENTITY employer "Synthetic Expanded Employer">]>',
             "&employer;: Engineer", "Python systems."),
            ('<!DOCTYPE rss [<!ENTITY a "small"><!ENTITY b "&a;&a;&a;">]>',
             "Synthetic Employer: Engineer", "&b;"),
            ('<!DOCTYPE rss SYSTEM "https://example.invalid/fixture.dtd">',
             "Synthetic Employer: Engineer", "Python systems."),
            ('<!DOCTYPE rss PUBLIC "synthetic fixture" "https://example.invalid/fixture.dtd">',
             "Synthetic Employer: Engineer", "Python systems."),
        )
        for declaration, title, description in declarations:
            for encoding, codec in ENCODINGS:
                with self.subTest(declaration=declaration, encoding=encoding, codec=codec):
                    adapter, visited, stream = self.adapter(rss(
                        declaration=declaration, title=title, description=description,
                        encoding=encoding, codec=codec,
                    ))
                    with self.assertRaises(SourceError) as failure:
                        adapter.search()
                    self.assertEqual(failure.exception.code, "SOURCE_PAYLOAD_INVALID")
                    self.assertEqual(str(failure.exception), "SOURCE_PAYLOAD_INVALID")
                    self.assertEqual(adapter.health(), "SOURCE_PAYLOAD_INVALID")
                    self.assertEqual(visited, [FEED_URL])
                    self.assertTrue(stream.closed)

    def test_accepts_normal_xml_encodings_and_predefined_or_numeric_entities(self):
        for encoding, codec in ENCODINGS:
            with self.subTest(encoding=encoding, codec=codec):
                adapter, visited, stream = self.adapter(rss(
                    title="Synthetic &amp; Company: Engineer",
                    description="Python &#38; SQL &#x2022; systems.",
                    encoding=encoding, codec=codec,
                ))
                batch = adapter.search()
                self.assertEqual(len(batch.items), 1)
                self.assertEqual(batch.items[0].raw["title"], "Synthetic & Company: Engineer")
                self.assertEqual(batch.items[0].raw["description"], "Python & SQL \u2022 systems.")
                self.assertEqual(adapter.health(), "HEALTHY")
                self.assertEqual(visited, [FEED_URL])
                self.assertTrue(stream.closed)

    def test_declaration_text_in_comments_and_cdata_is_not_a_doctype(self):
        adapter, visited, stream = self.adapter(rss(
            declaration="<!-- A literal <!DOCTYPE rss> in a comment is harmless. -->",
            description='<![CDATA[Literal <!ENTITY example "text"> and <!DOCTYPE rss>.]]>',
        ))
        batch = adapter.search()
        self.assertEqual(
            batch.items[0].raw["description"],
            'Literal <!ENTITY example "text"> and <!DOCTYPE rss>.',
        )
        self.assertEqual(adapter.health(), "HEALTHY")
        self.assertEqual(visited, [FEED_URL])
        self.assertTrue(stream.closed)

    def test_unsupported_encoding_is_classified_without_leaking_parser_details(self):
        for encoding in ("unknown-synthetic-encoding", "utf-7"):
            with self.subTest(encoding=encoding):
                adapter, visited, stream = self.adapter(rss(
                    encoding=encoding, codec="utf-8",
                ))
                with self.assertRaises(SourceError) as failure:
                    adapter.search()
                self.assertEqual(failure.exception.code, "SOURCE_PAYLOAD_INVALID")
                self.assertEqual(str(failure.exception), "SOURCE_PAYLOAD_INVALID")
                self.assertEqual(adapter.health(), "SOURCE_PAYLOAD_INVALID")
                self.assertEqual(visited, [FEED_URL])
                self.assertTrue(stream.closed)

    def test_a_rejected_document_does_not_poison_a_later_valid_collection(self):
        payloads = iter((rss(declaration="<!DOCTYPE rss>"), rss()))
        visited = []

        def respond(request):
            visited.append(str(request.url))
            return httpx.Response(200, content=next(payloads))

        adapter = WeWorkRemotely(transport=httpx.MockTransport(respond))
        with self.assertRaises(SourceError):
            adapter.search()
        self.assertEqual(adapter.health(), "SOURCE_PAYLOAD_INVALID")
        self.assertEqual(len(adapter.search().items), 1)
        self.assertEqual(adapter.health(), "HEALTHY")
        self.assertEqual(visited, [FEED_URL, FEED_URL])


class WeWorkRemotelyXmlPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{(Path(self.directory.name) / 'jobs.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.save_profile(PROFILE)
        self.source = configure_source(self.store, "default", provider="wwr")

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def collect(self, payload):
        # Reset only the isolated fixture's admission timestamps to exercise retry.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == self.source,
            ).values(last_attempt=None, backoff_until=None))
        return collect_source(self.store, self.source, "fixture", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=payload),
        ))

    @staticmethod
    def rows(store):
        with store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(
                    select(table).order_by(table.c.id),
                ).mappings()]
                for table in (m.companies, m.jobs, m.routes, m.postings,
                              m.posting_snapshots, m.posting_value_candidates)
            }

    def test_parser_rejection_preserves_prior_jobs_and_recovers_after_database_reopen(self):
        self.collect(rss())
        before = self.rows(self.store)
        invalid = (
            rss(declaration='<!DOCTYPE rss [<!ENTITY employer "Unaccepted Employer">]>',
                title="&employer;: Unaccepted Engineer", slug="must-not-be-imported"),
            rss(encoding="unknown-synthetic-encoding", codec="utf-8",
                title="Unaccepted Employer: Engineer", slug="must-not-be-imported"),
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(SourceError) as failure:
                    self.collect(payload)
                self.assertEqual(failure.exception.code, "SOURCE_PAYLOAD_INVALID")
                self.assertEqual(self.rows(self.store), before)
                with self.store.engine.connect() as connection:
                    state = connection.execute(select(m.job_sources).where(
                        m.job_sources.c.id == self.source,
                    )).mappings().one()
                self.assertEqual(state["health_status"], "FAILED")
                self.assertEqual(state["last_error"], "SOURCE_PAYLOAD_INVALID")
                reopened = Store(self.database_url)
                try:
                    self.assertEqual(self.rows(reopened), before)
                finally:
                    reopened.engine.dispose()

        self.collect(rss())
        self.assertEqual(self.store.count("canonical_jobs"), 1)
        self.assertEqual(self.store.count("job_postings"), 1)
        with self.store.engine.connect() as connection:
            state = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == self.source,
            )).mappings().one()
        self.assertEqual(state["health_status"], "HEALTHY")
        self.assertIsNone(state["last_error"])


if __name__ == "__main__":
    unittest.main()
