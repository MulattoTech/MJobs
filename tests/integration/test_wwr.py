"""Integration and contract tests for the We Work Remotely global job-source adapter (MJ-REQ-017.S02)."""
from pathlib import Path
import tempfile
import unittest

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.contract import (
    CollectionStrategy, SearchBatch, SourceCapabilities, SourceCapability,
    SourceCompany, SourceItem,
)
from services.discovery.sources.wwr import WeWorkRemotely, SourceError, _extract_company_title, _posting_id_from_guid
from services.discovery.sources.collect import configure_source, collect_source
from services.discovery.sources.registry import source_adapter
from tests.integration.test_spine import PROFILE

# ── Fixture RSS payload mimicking the live We Work Remotely feed ──────
RAW_WWR_RSS = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss">
  <channel>
    <title>We Work Remotely: Remote jobs in design, programming, marketing and more</title>
    <link>https://weworkremotely.com/remote-jobs.rss</link>
    <description>We Work Remotely</description>
    <language>en-US</language>
    <ttl>60</ttl>
    <item>
      <media:content url="https://wwr-pro.s3.amazonaws.com/logos/0001/0001/logo.gif" type="image/png"/>
      <title>Acme Cloud: Senior Python Engineer</title>
      <region>Anywhere in the World</region>
      <country>&#x1F1FA;&#x1F1F8; United States of America</country>
      <state>California</state>
      <skills>Python, SQL, REST, Backend</skills>
      <category>Full-Stack Programming</category>
      <type>Full-Time</type>
      <description>&lt;p&gt;Build high-scale services using Python, SQL, and REST APIs. &amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;&lt;/p&gt;&lt;p&gt;The salary range is $130,000 - $160,000 per year.&lt;/p&gt;</description>
      <pubDate>Tue, 06 Oct 2026 12:00:00 +0000</pubDate>
      <expires_at>Thu, 05 Nov 2026 12:00:00 +0000</expires_at>
      <guid>https://weworkremotely.com/remote-jobs/acme-cloud-senior-python-engineer</guid>
      <link>https://weworkremotely.com/remote-jobs/acme-cloud-senior-python-engineer</link>
    </item>
    <item>
      <title>Globex Systems: Frontend Engineer</title>
      <region>Europe Only</region>
      <country></country>
      <state></state>
      <skills>React, TypeScript</skills>
      <category>Front-End Programming</category>
      <type>Contract</type>
      <description>&lt;p&gt;Develop reactive user interfaces using TypeScript.&lt;/p&gt;</description>
      <pubDate>Mon, 05 Oct 2026 15:30:00 +0000</pubDate>
      <expires_at>Wed, 04 Nov 2026 15:30:00 +0000</expires_at>
      <guid>https://weworkremotely.com/remote-jobs/globex-systems-frontend-engineer</guid>
      <link>https://weworkremotely.com/remote-jobs/globex-systems-frontend-engineer</link>
    </item>
    <item>
      <title>No-Company Title Only</title>
      <region></region>
      <country></country>
      <state></state>
      <skills></skills>
      <category>All Other Remote</category>
      <type>Full-Time</type>
      <description>&lt;p&gt;A simple job posting without company separator.&lt;/p&gt;</description>
      <pubDate>Sun, 04 Oct 2026 08:00:00 +0000</pubDate>
      <expires_at></expires_at>
      <guid>https://weworkremotely.com/remote-jobs/no-company-title-only</guid>
      <link>https://weworkremotely.com/remote-jobs/no-company-title-only</link>
    </item>
  </channel>
</rss>
"""


class WeWorkRemotelyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile(PROFILE)
        self.requests = []

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def transport(self, request):
        self.requests.append(request)
        self.assertEqual(request.method, "GET")
        self.assertIn("weworkremotely.com", str(request.url.host))
        return httpx.Response(200, content=RAW_WWR_RSS, headers={"content-type": "application/rss+xml"})

    # ── Contract tests ──────────────────────────────────────────────────

    def test_search_contract_and_multi_company_attribution(self):
        adapter = WeWorkRemotely(transport=httpx.MockTransport(self.transport))
        batch = adapter.search()
        self.assertEqual(batch.endpoint, "https://weworkremotely.com/remote-jobs.rss")
        self.assertEqual(len(batch.items), 3)

        first = batch.items[0]
        self.assertEqual(first.company.name, "Acme Cloud")
        self.assertEqual(first.company.identity_parts, ("wwr", "acme-cloud"))

        second = batch.items[1]
        self.assertEqual(second.company.name, "Globex Systems")
        self.assertEqual(second.company.identity_parts, ("wwr", "globex-systems"))

        # Third item has no colon separator → "Unknown Company"
        third = batch.items[2]
        self.assertEqual(third.company.name, "Unknown Company")
        self.assertEqual(third.company.identity_parts, ("wwr", "unknown-company"))

        self.assertIsNone(batch.source_company)
        self.assertEqual(adapter.health(), "HEALTHY")

    def test_normalize_maps_to_canonical_job_fields(self):
        adapter = WeWorkRemotely(transport=httpx.MockTransport(self.transport))
        batch = adapter.search()
        normalized = adapter.normalize(batch.items[0].raw)

        self.assertEqual(normalized["requisition"], "acme-cloud-senior-python-engineer")
        self.assertEqual(normalized["title"], "Senior Python Engineer")
        self.assertIn("Python", normalized["description"])
        self.assertNotIn("<p>", normalized["description"])
        self.assertNotIn("<script>", normalized["description"])
        self.assertEqual(normalized["remote_type"], "REMOTE")
        self.assertEqual(normalized["location"], "Anywhere in the World")
        self.assertEqual(normalized["application_url"], "https://weworkremotely.com/remote-jobs/acme-cloud-senior-python-engineer")

        attrs = normalized["attributes"]
        self.assertEqual(attrs["provider"], "wwr")
        self.assertEqual(attrs["company_name"], "Acme Cloud")
        self.assertEqual(attrs["native_posting_id"], "acme-cloud-senior-python-engineer")
        self.assertEqual(attrs["employment_type"], "FULL_TIME")
        self.assertEqual(attrs["category"], "Software Development")
        self.assertIn("Python", normalized["skills"])

    def test_normalize_second_item_contract_type(self):
        adapter = WeWorkRemotely(transport=httpx.MockTransport(self.transport))
        batch = adapter.search()
        normalized = adapter.normalize(batch.items[1].raw)

        self.assertEqual(normalized["requisition"], "globex-systems-frontend-engineer")
        self.assertEqual(normalized["title"], "Frontend Engineer")
        attrs = normalized["attributes"]
        self.assertEqual(attrs["employment_type"], "CONTRACT")
        self.assertEqual(attrs["category"], "Software Development")

    def test_normalize_item_without_company_separator(self):
        adapter = WeWorkRemotely(transport=httpx.MockTransport(self.transport))
        batch = adapter.search()
        normalized = adapter.normalize(batch.items[2].raw)

        self.assertEqual(normalized["requisition"], "no-company-title-only")
        self.assertEqual(normalized["title"], "No-Company Title Only")
        attrs = normalized["attributes"]
        self.assertEqual(attrs["company_name"], "Unknown Company")
        self.assertEqual(attrs["category"], "All Others")

    def test_capabilities_and_unsupported_features(self):
        adapter = WeWorkRemotely()
        caps = adapter.capabilities()
        self.assertEqual(caps.strategy, CollectionStrategy.FEED)
        self.assertTrue(caps.supports(SourceCapability.DISCOVERY))
        self.assertTrue(caps.supports(SourceCapability.JOB_DETAIL))
        self.assertTrue(caps.supports(SourceCapability.REMOTE_METADATA))
        self.assertTrue(caps.supports(SourceCapability.APPLICATION_REDIRECT))
        # Explicitly unsupported
        self.assertFalse(caps.supports(SourceCapability.DIRECT_APPLICATION))
        self.assertFalse(caps.supports(SourceCapability.APPLICATION_STATUS))
        self.assertFalse(caps.supports(SourceCapability.SALARY))

    def test_fetch_job_by_slug(self):
        adapter = WeWorkRemotely(transport=httpx.MockTransport(self.transport))
        raw = adapter.fetch("acme-cloud-senior-python-engineer")
        self.assertIn("Senior Python Engineer", raw["title"])

        with self.assertRaises(SourceError):
            adapter.fetch("nonexistent-job-slug-9999")

    # ── Error handling ──────────────────────────────────────────────────

    def test_error_handling_and_health_states(self):
        call_count = [0]
        def error_transport(request):
            call_count[0] += 1
            if call_count[0] == 1:
                return httpx.Response(500, text="Server Error")
            if call_count[0] == 2:
                return httpx.Response(429, headers={"retry-after": "45"}, text="Rate limited")
            if call_count[0] == 3:
                return httpx.Response(200, text="not valid xml <><>")
            raise httpx.ConnectTimeout("connection timed out")

        adapter = WeWorkRemotely(transport=httpx.MockTransport(error_transport))

        # 500 error
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_SERVER_ERROR")
        self.assertEqual(adapter.health(), "SOURCE_SERVER_ERROR")

        # 429 error
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_RATE_LIMITED")
        self.assertEqual(cm.exception.retry_seconds, 45)
        self.assertEqual(adapter.health(), "SOURCE_RATE_LIMITED")

        # Invalid XML
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_PAYLOAD_INVALID")

        # Timeout
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_TIMEOUT")
        self.assertEqual(adapter.health(), "SOURCE_TIMEOUT")

    def test_invalid_rss_missing_channel(self):
        """RSS that parses as XML but has no <channel> element."""
        def bad_transport(request):
            return httpx.Response(200, content=b'<?xml version="1.0"?><rss><broken/></rss>')
        adapter = WeWorkRemotely(transport=httpx.MockTransport(bad_transport))
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_PAYLOAD_INVALID")

    # ── Client-side filtering ───────────────────────────────────────────

    def test_search_with_category_filter(self):
        adapter = WeWorkRemotely(transport=httpx.MockTransport(self.transport))
        batch = adapter.search(category="Full-Stack")
        # Only items whose category contains "Full-Stack" (case-insensitive)
        self.assertEqual(len(batch.items), 1)
        self.assertEqual(batch.items[0].company.name, "Acme Cloud")

    def test_search_with_search_term(self):
        adapter = WeWorkRemotely(transport=httpx.MockTransport(self.transport))
        batch = adapter.search(search_term="TypeScript")
        self.assertEqual(len(batch.items), 1)
        self.assertEqual(batch.items[0].company.name, "Globex Systems")

    def test_search_with_limit(self):
        adapter = WeWorkRemotely(transport=httpx.MockTransport(self.transport))
        batch = adapter.search(limit=1)
        self.assertEqual(len(batch.items), 1)

    # ── Cancellation ────────────────────────────────────────────────────

    def test_cancellation_before_request(self):
        cancelled = True
        def check():
            if cancelled:
                raise KeyboardInterrupt("cancelled")

        adapter = WeWorkRemotely(transport=httpx.MockTransport(self.transport), check=check)
        with self.assertRaises(KeyboardInterrupt):
            adapter.search()

    # ── Registry integration ────────────────────────────────────────────

    def test_registry_integration(self):
        policy = {"provider": "wwr", "tenant": "default", "request_timeout": 15, "rate_limit": 30, "concurrency": 2}
        adapter = source_adapter(policy)
        self.assertIsInstance(adapter, WeWorkRemotely)
        self.assertEqual(adapter.request_timeout, 15)
        self.assertEqual(adapter.rate_limit, 30)
        self.assertEqual(adapter.concurrency, 2)

    # ── Persistence integration (end-to-end with collect_source) ──────

    def test_collect_source_persists_multi_company_jobs(self):
        source_id = configure_source(self.store, "default", provider="wwr", poll_interval=300)
        self.assertTrue(source_id)

        collect_source(self.store, source_id, "fixture", transport=httpx.MockTransport(self.transport))

        with self.store.engine.connect() as conn:
            companies = conn.execute(select(m.companies)).mappings().all()
            company_names = {c["name"] for c in companies}
            self.assertIn("Acme Cloud", company_names)
            self.assertIn("Globex Systems", company_names)
            self.assertIn("Unknown Company", company_names)

            postings = conn.execute(select(m.postings)).mappings().all()
            self.assertEqual(len(postings), 3)

            snapshots = conn.execute(select(m.posting_snapshots)).mappings().all()
            self.assertEqual(len(snapshots), 3)

            source_row = conn.execute(select(m.job_sources).where(m.job_sources.c.id == source_id)).mappings().one()
            self.assertEqual(source_row["health_status"], "HEALTHY")
            self.assertEqual(source_row["provider"], "wwr")

    # ── Helper unit tests ───────────────────────────────────────────────

    def test_extract_company_title_with_colon(self):
        self.assertEqual(_extract_company_title("Acme: Senior Engineer"), ("Acme", "Senior Engineer"))

    def test_extract_company_title_without_colon(self):
        self.assertEqual(_extract_company_title("Senior Engineer"), ("Unknown Company", "Senior Engineer"))

    def test_extract_company_title_empty_after_colon(self):
        self.assertEqual(_extract_company_title("Acme:"), ("Unknown Company", "Acme:"))

    def test_posting_id_from_guid(self):
        self.assertEqual(
            _posting_id_from_guid("https://weworkremotely.com/remote-jobs/acme-cloud-senior-python-engineer"),
            "acme-cloud-senior-python-engineer"
        )

    def test_posting_id_from_guid_trailing_slash(self):
        self.assertEqual(
            _posting_id_from_guid("https://weworkremotely.com/remote-jobs/test-slug/"),
            "test-slug"
        )

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            WeWorkRemotely(request_timeout=1)
        with self.assertRaises(ValueError):
            WeWorkRemotely(rate_limit=0)
        with self.assertRaises(ValueError):
            WeWorkRemotely(concurrency=0)

    def test_live_bounded_search_read_only(self):
        """Bounded live read-only verification against WWR public RSS feed when enabled."""
        import os
        if not os.environ.get("MJOBS_ENABLE_LIVE_TESTS"):
            return
        adapter = WeWorkRemotely(request_timeout=15)
        batch = adapter.search(limit=3)
        self.assertGreater(len(batch.items), 0)
        self.assertLessEqual(len(batch.items), 3)
        for item in batch.items:
            norm = adapter.normalize(item.raw)
            self.assertTrue(norm["title"])
            self.assertTrue(norm["requisition"])
            self.assertEqual(norm["remote_type"], "REMOTE")
            self.assertEqual(norm["attributes"]["provider"], "wwr")


if __name__ == "__main__":
    unittest.main()
