"""Integration and contract tests for the We Work Remotely global job-source adapter (MJ-REQ-017.S02)."""
from pathlib import Path
from copy import deepcopy
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

import httpx
from fastapi.testclient import TestClient
from workers.operations import OperationCancelled
from workers.tasks import execute_operation
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from apps.api.main import create_app
from services.discovery.search_history import list_search_history
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
        self.store.bind_workspace_mode("FIXTURE")
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

    def test_fixed_feed_rejects_redirects_without_visiting_location(self):
        # The fixed public feed must not let a provider redirect the worker to
        # an arbitrary destination. MockTransport keeps every case synthetic;
        # a second recorded request means HTTPX followed the redirect.
        locations = (
            "https://127.0.0.1/private",
            "https://10.0.0.1/private",
            "https://169.254.169.254/latest/meta-data/",
            "http://weworkremotely.com/remote-jobs.rss",
            "https://attacker.example/remote-jobs.rss",
            "https://weworkremotely.com:8443/remote-jobs.rss",
        )
        feed_url = "https://weworkremotely.com/remote-jobs.rss"

        for location in locations:
            with self.subTest(location=location):
                visited = []

                def respond(request):
                    visited.append(str(request.url))
                    if len(visited) == 1:
                        return httpx.Response(302, headers={"location": location})
                    # If followed, make the request appear successful so the
                    # test detects the unwanted destination visit.
                    return httpx.Response(200, content=RAW_WWR_RSS)

                adapter = WeWorkRemotely(transport=httpx.MockTransport(respond))
                with self.assertRaises(SourceError) as raised:
                    adapter.search()

                self.assertEqual(raised.exception.code, "SOURCE_UNAVAILABLE")
                self.assertEqual(visited, [feed_url])
                self.assertEqual(adapter.health(), "SOURCE_UNAVAILABLE")

    def test_redirect_disabled_policy_accepts_normal_fixed_feed_response(self):
        visited = []

        def respond(request):
            visited.append(str(request.url))
            return httpx.Response(200, content=RAW_WWR_RSS,
                                  headers={"content-type": "application/rss+xml"})

        adapter = WeWorkRemotely(transport=httpx.MockTransport(respond))
        batch = adapter.search()

        self.assertEqual(visited, ["https://weworkremotely.com/remote-jobs.rss"])
        self.assertEqual(len(batch.items), 3)
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

    @staticmethod
    def duplicate_fixture(rows):
        root = ElementTree.Element('rss', version='2.0')
        channel = ElementTree.SubElement(root, 'channel')
        for row in rows:
            item = ElementTree.SubElement(channel, 'item')
            for key, value in row.items():
                ElementTree.SubElement(item, key).text = value
        return ElementTree.tostring(root, encoding='utf-8')

    def test_fetch_rejects_conflicting_identity_independent_of_order(self):
        base = {'title': 'Synthetic Acme: Engineer', 'description': 'Python systems.',
                'guid': 'https://weworkremotely.com/remote-jobs/synthetic-identity',
                'link': 'https://weworkremotely.com/remote-jobs/synthetic-identity'}
        for key, value in [('title', 'Synthetic Acme: Different Engineer'),
                           ('description', 'Different work.'), ('category', 'Different category')]:
            for reverse in (False, True):
                with self.subTest(field=key, reverse=reverse):
                    rows = [base, {**base, key: value}]
                    payload = self.duplicate_fixture(rows[::-1] if reverse else rows)
                    adapter = WeWorkRemotely(transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, content=payload)))
                    with self.assertRaises(SourceError) as failure:
                        adapter.fetch('synthetic-identity')
                    self.assertEqual(failure.exception.code, 'SOURCE_DUPLICATE_JOB_ID')
                    self.assertEqual(adapter.health(), 'SOURCE_DUPLICATE_JOB_ID')
        payload = self.duplicate_fixture([base, deepcopy(base)])
        adapter = WeWorkRemotely(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=payload)))
        self.assertEqual(adapter.fetch('synthetic-identity')['description'], base['description'])
        self.assertEqual(adapter.health(), 'HEALTHY')

    def test_search_excludes_conflicts_before_filters_or_limits(self):
        """Filtering cannot restore a variant or hide the adapter's partial health."""
        base = {'title': 'Synthetic Acme: Engineer', 'description': 'Original work.',
                'guid': 'https://weworkremotely.com/remote-jobs/duplicate-identity',
                'link': 'https://weworkremotely.com/remote-jobs/duplicate-identity',
                'category': 'Programming'}
        conflicting = {**base, 'description': 'Different work.'}
        fresh = {'title': 'Fresh Only: Designer', 'description': 'Unrelated role.',
                 'guid': 'https://weworkremotely.com/remote-jobs/fresh-identity',
                 'link': 'https://weworkremotely.com/remote-jobs/fresh-identity',
                 'category': 'Design'}

        for reverse in (False, True):
            for kwargs, expected in (({}, [fresh['link']]), ({'limit': 1}, [fresh['link']]),
                                     ({'category': 'Design'}, [fresh['link']]),
                                     ({'search_term': 'Fresh Only'}, [fresh['link']]),
                                     ({'category': 'Programming'}, []),
                                     ({'search_term': 'Different work'}, [])):
                with self.subTest(reverse=reverse, filter=kwargs):
                    pair = [conflicting, base] if reverse else [base, conflicting]
                    payload = self.duplicate_fixture([fresh, *pair])
                    adapter = WeWorkRemotely(transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, content=payload)))
                    batch = adapter.search(**kwargs)
                    self.assertEqual([item.raw['link'] for item in batch.items], expected)
                    self.assertEqual(adapter.health(), 'PARTIAL')

    def test_search_coalesces_only_exact_repeats_and_preserves_first_seen_order(self):
        first = {'title': 'Synthetic Acme: Engineer', 'description': 'Same role.',
                 'guid': 'https://weworkremotely.com/remote-jobs/first-identity',
                 'link': 'https://weworkremotely.com/remote-jobs/first-identity'}
        second = {'title': 'Synthetic Other: Designer', 'description': 'Other role.',
                  'guid': 'https://weworkremotely.com/remote-jobs/second-identity',
                  'link': 'https://weworkremotely.com/remote-jobs/second-identity'}
        payload = self.duplicate_fixture([first, second, deepcopy(first), deepcopy(second)])
        adapter = WeWorkRemotely(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=payload)))
        batch = adapter.search()
        self.assertEqual([_posting_id_from_guid(item.raw['guid']) for item in batch.items],
                         ['first-identity', 'second-identity'])
        self.assertEqual(adapter.health(), 'HEALTHY')
        limited = adapter.search(limit=1)
        self.assertEqual(len(limited.items), 1)
        self.assertEqual(limited.items[0].raw['description'], 'Same role.')

    def test_collection_preserves_prior_posting_and_admits_only_unambiguous_rows(self):
        base = {'title': 'Synthetic Acme: Engineer', 'description': 'Python systems.',
                'guid': 'https://weworkremotely.com/remote-jobs/synthetic-identity',
                'link': 'https://weworkremotely.com/remote-jobs/synthetic-identity'}
        fresh = {**base, 'title': 'Synthetic Other: New role',
                 'guid': 'https://weworkremotely.com/remote-jobs/new-synthetic-identity',
                 'link': 'https://weworkremotely.com/remote-jobs/new-synthetic-identity'}
        source = configure_source(self.store, 'default', provider='wwr')

        def collect(rows):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.job_sources).where(m.job_sources.c.id == source)
                                   .values(last_attempt=None, backoff_until=None))
            payload = self.duplicate_fixture(rows)
            return collect_source(self.store, source, 'fixture', transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=payload)))

        def persisted_rows():
            with self.store.engine.connect() as connection:
                return {table.name: [dict(row) for row in connection.execute(
                    select(table).order_by(table.c.id)).mappings()] for table in (
                        m.companies, m.jobs, m.routes, m.postings, m.posting_snapshots,
                        m.posting_value_candidates)}

        collect([base])
        before = persisted_rows()
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                duplicate = [base, {**base, 'description': 'Conflicting work.'}]
                with self.assertRaises(SourceError) as failure:
                    collect(duplicate[::-1] if reverse else duplicate)
                self.assertEqual(failure.exception.code, 'SOURCE_DUPLICATE_JOB_ID')
                self.assertEqual(persisted_rows(), before)
                with self.store.engine.connect() as connection:
                    state = connection.execute(select(m.job_sources).where(
                        m.job_sources.c.id == source)).mappings().one()
                self.assertEqual(state['health_status'], 'FAILED')
                self.assertEqual(state['last_error'], 'SOURCE_DUPLICATE_JOB_ID')
        result = collect([fresh, *duplicate])
        self.assertEqual(result['jobs_seen'], 1)
        after = persisted_rows()
        for table in (m.postings, m.posting_snapshots, m.posting_value_candidates):
            for row in before[table.name]:
                self.assertIn(row, after[table.name])
        self.assertEqual(self.store.count('job_postings'), 2)

    def test_mixed_feed_discovery_records_quarantined_id_as_partial(self):
        base = {"title": "Acme: Engineer", "link": "https://weworkremotely.com/remote-jobs/acme"}
        ambiguous = {"title": "Example: Engineer", "link": "https://weworkremotely.com/remote-jobs/ambiguous"}
        payload = self.duplicate_fixture([base, ambiguous, base, {**ambiguous, "description": "Different"}])
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        source_id = configure_source(self.store, "default", provider="wwr")
        with TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                   operation_mode="FIXTURE")) as client:
            queued = client.post(f"/sources/{source_id}/collect",
                                 json={"profile_id": "fixture", "request_id": "wwr-mixed-feed"})
            self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]

        def local_adapter(source, **options):
            return source_adapter(source, **{**options, "transport": transport})

        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": self.store.url, "MJOBS_OPERATION_MODE": "FIXTURE"}), \
             patch("services.discovery.sources.collect.source_adapter", side_effect=local_adapter):
            execute_operation(operation_id)

        with self.store.engine.connect() as db:
            operation = db.execute(select(m.operations.c.state, m.operations.c.error).where(
                m.operations.c.id == operation_id)).one()
            source = db.execute(select(m.job_sources.c.health_status, m.job_sources.c.jobs_seen,
                                       m.job_sources.c.last_error).where(m.job_sources.c.id == source_id)).one()
            quality = db.execute(select(m.source_quality_observations.c.enumerated,
                                        m.source_quality_observations.c.accepted,
                                        m.source_quality_observations.c.succeeded).where(
                m.source_quality_observations.c.configured_source_id == source_id)).one()
            audit = db.execute(select(m.audit.c.payload).where(m.audit.c.entity_id == source_id,
                m.audit.c.action == "SOURCE_COLLECTION_COMPLETE")).scalar_one()
            postings = db.execute(select(m.postings.c.application_url)).scalars().all()
        self.assertEqual(postings, [base["link"]])
        history, = list_search_history(self.store, "fixture")["runs"]
        self.assertEqual((history["status"], history["results"], history["new_jobs"], history["duplicates"]),
                         ("COMPLETED", 1, 1, None))
        # A complete operation can yield a partial source collection. Feed
        # repeats count once, and the quarantined identity counts as rejected.
        self.assertEqual(
            {"operation": tuple(operation), "source": tuple(source), "quality": tuple(quality), "audit": audit},
            {"operation": ("COMPLETED", None), "source": ("PARTIAL", 1, None), "quality": (2, 1, True),
             "audit": {"jobs_seen": 1, "rejected": 1, "provider": "wwr"}},
        )

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

    def test_collect_uses_bounded_structured_location_over_generic_description(self):
        from services.discovery.matching import build_preference_fit_factors
        from services.discovery.search_semantics import evaluate_search
        from services.discovery.search_profiles import get_search_profile, save_search_profile

        generic = "Remote opportunity; generic feed copy says available worldwide."
        rows = [
            {"title": "US Scope Co: Engineer", "region": "USA Only",
             "country": "🇺🇸 United States of America", "state": "California",
             "description": generic, "guid": "https://weworkremotely.com/remote-jobs/us-scope",
             "link": "https://weworkremotely.com/remote-jobs/us-scope"},
            {"title": "World Scope Co: Engineer", "region": "Anywhere in the World",
             "country": "United States", "state": "California", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/world-scope",
             "link": "https://weworkremotely.com/remote-jobs/world-scope"},
            {"title": "Country Scope Co: Engineer", "region": "", "country": "Canada",
             "state": "", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/country-scope",
             "link": "https://weworkremotely.com/remote-jobs/country-scope"},
            {"title": "California Scope Co: Engineer", "region": "California", "country": "United States",
             "state": "California", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/california-scope",
             "link": "https://weworkremotely.com/remote-jobs/california-scope"},
            {"title": "State Scope Co: Engineer", "region": "", "country": "",
             "state": "California", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/state-scope",
             "link": "https://weworkremotely.com/remote-jobs/state-scope"},
            {"title": "Unsupported Scope Co: Engineer", "region": "Europe Only",
             "country": "United States", "state": "California", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/unsupported-scope",
             "link": "https://weworkremotely.com/remote-jobs/unsupported-scope"},
            {"title": "Conflicting Scope Co: Engineer", "region": "USA Only",
             "country": "Canada", "state": "", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/conflicting-scope",
             "link": "https://weworkremotely.com/remote-jobs/conflicting-scope"},
            {"title": "Excluded Scope Co: Engineer", "region": "Not USA",
             "country": "United States", "state": "California", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/excluded-scope",
             "link": "https://weworkremotely.com/remote-jobs/excluded-scope"},
            {"title": "Ambiguous Scope Co: Engineer", "region": "CA Only",
             "country": "", "state": "", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/ambiguous-scope",
             "link": "https://weworkremotely.com/remote-jobs/ambiguous-scope"},
            {"title": "Malformed Scope Co: Engineer", "region": "U" * 129,
             "country": "United States", "state": "California", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/malformed-scope",
             "link": "https://weworkremotely.com/remote-jobs/malformed-scope"},
            {"title": "Known Control Co: Engineer", "region": "United Kingdom", "country": "UK",
             "state": "", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/known-control",
             "link": "https://weworkremotely.com/remote-jobs/known-control"},
            {"title": "Unicode Exclusion Co: Engineer", "region": "Worldwide 🌎 except USA",
             "country": "United States", "state": "California", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/unicode-exclusion",
             "link": "https://weworkremotely.com/remote-jobs/unicode-exclusion"},
            {"title": "Punctuation Exclusion Co: Engineer", "region": "Worldwide—except USA",
             "country": "United States", "state": "California", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/punctuation-exclusion",
             "link": "https://weworkremotely.com/remote-jobs/punctuation-exclusion"},
            {"title": "Japanese Exclusion Co: Engineer", "region": "Worldwide 米国を除く",
             "country": "United States", "state": "California", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/japanese-exclusion",
             "link": "https://weworkremotely.com/remote-jobs/japanese-exclusion"},
            {"title": "Canadian Flag Control Co: Engineer", "region": "", "country": "🇨🇦 Canada",
             "state": "", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/canada-flag-control",
             "link": "https://weworkremotely.com/remote-jobs/canada-flag-control"},
            {"title": "Mismatched Flag Control Co: Engineer", "region": "", "country": "🇨🇦 United States",
             "state": "", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/mismatched-flag-control",
             "link": "https://weworkremotely.com/remote-jobs/mismatched-flag-control"},
            {"title": "Reverse Mismatched Flag Control Co: Engineer", "region": "", "country": "🇺🇸 Canada",
             "state": "", "description": generic,
             "guid": "https://weworkremotely.com/remote-jobs/reverse-mismatched-flag-control",
             "link": "https://weworkremotely.com/remote-jobs/reverse-mismatched-flag-control"},
        ]
        payload = self.duplicate_fixture(rows)
        # Real XML shapes which Element.findtext() used to accept partially.
        malformed_nodes = b'''\
          <item><title>Duplicate Scope Co: Engineer</title><region>USA Only</region><region>Canada Only</region>
            <country>United States</country><state>California</state><description>Remote opportunity.</description>
            <guid>https://weworkremotely.com/remote-jobs/duplicate-region</guid><link>https://weworkremotely.com/remote-jobs/duplicate-region</link></item>
          <item><title>Nested Scope Co: Engineer</title><region>Worldwide<exception>except USA</exception></region>
            <country>United States</country><state>California</state><description>Remote opportunity.</description>
            <guid>https://weworkremotely.com/remote-jobs/nested-region</guid><link>https://weworkremotely.com/remote-jobs/nested-region</link></item>
          <item><title>Tail Scope Co: Engineer</title><region>Worldwide</region> except USA
            <country>United States</country><state>California</state><description>Remote opportunity.</description>
            <guid>https://weworkremotely.com/remote-jobs/tail-region</guid><link>https://weworkremotely.com/remote-jobs/tail-region</link></item>
        '''
        payload = payload.replace(b"</channel>", malformed_nodes + b"</channel>")
        source_id = configure_source(self.store, "default", provider="wwr", poll_interval=300)
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=payload,
            headers={"content-type": "application/rss+xml"}))
        collect_source(self.store, source_id, "fixture", transport=transport)

        with self.store.engine.connect() as connection:
            jobs = {row["requisition"]: dict(row) for row in connection.execute(
                select(m.jobs).where(m.jobs.c.requisition.in_([
                    "us-scope", "world-scope", "country-scope", "california-scope", "state-scope",
                    "unsupported-scope", "conflicting-scope", "excluded-scope",
                    "ambiguous-scope", "malformed-scope", "known-control", "duplicate-region",
                    "nested-region", "tail-region", "unicode-exclusion", "punctuation-exclusion",
                    "japanese-exclusion", "canada-flag-control", "mismatched-flag-control",
                    "reverse-mismatched-flag-control",
                ]))).mappings()}
        self.assertEqual(set(jobs), {
            "us-scope", "world-scope", "country-scope", "state-scope",
            "california-scope",
            "unsupported-scope", "conflicting-scope", "excluded-scope",
            "ambiguous-scope", "malformed-scope", "known-control", "duplicate-region",
            "nested-region", "tail-region", "unicode-exclusion", "punctuation-exclusion",
            "japanese-exclusion", "canada-flag-control", "mismatched-flag-control",
            "reverse-mismatched-flag-control",
        })

        us_job = jobs["us-scope"]
        self.assertEqual(us_job["remote_type"], "REMOTE")
        self.assertEqual(us_job["attributes"]["remote_regions"], ["US"])
        self.assertEqual(us_job["attributes"]["country"], "US")
        self.assertEqual(us_job["attributes"]["state"], "CA")
        self.assertTrue(evaluate_search({"countries": ["US"], "states": ["CA"],
                                         "remote_regions": ["US"]}, us_job)["matched"])
        canada_factor = next(factor for factor in build_preference_fit_factors(
            us_job, {"contact": {"country": "Canada"}, "preferences": {}})
            if factor["id"] == "remote_location")
        self.assertEqual(canada_factor["status"], "NO_DIRECT_MATCH")

        world = jobs["world-scope"]["attributes"]
        self.assertEqual(world["remote_regions"], ["WORLDWIDE"])
        self.assertEqual(world["country"], "US")
        self.assertEqual(world["state"], "CA")
        self.assertEqual(jobs["country-scope"]["attributes"]["remote_regions"], ["CA"])
        self.assertEqual(jobs["state-scope"]["attributes"]["remote_regions"], [])
        self.assertEqual(jobs["state-scope"]["attributes"]["country"], None)
        california = jobs["california-scope"]["attributes"]
        self.assertEqual(california["remote_regions"], [])
        self.assertEqual(california["country"], "US")
        self.assertEqual(california["state"], "CA")
        self.assertEqual(jobs["known-control"]["attributes"]["remote_regions"], ["GB"])
        self.assertEqual(jobs["canada-flag-control"]["attributes"]["remote_regions"], ["CA"])
        for requisition in ("unsupported-scope", "conflicting-scope", "excluded-scope",
                            "ambiguous-scope", "malformed-scope", "duplicate-region",
                            "nested-region", "tail-region", "unicode-exclusion",
                            "punctuation-exclusion", "japanese-exclusion",
                            "mismatched-flag-control", "reverse-mismatched-flag-control"):
            with self.subTest(requisition=requisition):
                attributes = jobs[requisition]["attributes"]
                self.assertEqual(attributes["remote_regions"], [])
                self.assertIsNone(attributes["country"])
                self.assertIsNone(attributes["state"])

        # Exercise saved-profile readback through Store.jobs against persisted
        # rows, including positive and negative region/state selection.
        profiles = {
            "canada": {"remote_regions": ["CA"]},
            "united-states": {"remote_regions": ["US"]},
            "worldwide": {"remote_regions": ["WORLDWIDE"]},
            "california-state": {"countries": ["US"], "states": ["CA"]},
        }
        expected = {
            "canada": {"country-scope", "canada-flag-control"},
            "united-states": {"us-scope"},
            "worldwide": {"world-scope"},
            "california-state": {"us-scope", "california-scope", "world-scope"},
        }
        for name, filters in profiles.items():
            search_id = f"wwr-{name}"
            save_search_profile(self.store, {
                "id": search_id, "profile_id": "fixture", "name": f"WWR {name}",
                "enabled": True, **filters,
            })
            saved = get_search_profile(self.store, search_id)
            results = self.store.jobs(search_criteria=saved["criteria"], limit=100)
            matched = {row["requisition"] for row in results}
            self.assertEqual(matched, expected[name], name)

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
        if os.environ.get("MJOBS_ENABLE_LIVE_TESTS") != "1":
            self.skipTest("NOT_RUN: bounded live source checks require MJOBS_ENABLE_LIVE_TESTS=1")
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

    def test_payload_limit_rejected(self):
        class LargeStream(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(80):
                    yield b"x" * 65536

        adapter = WeWorkRemotely(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=LargeStream())))
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_PAYLOAD_LIMIT")
        self.assertEqual(adapter.health(), "SOURCE_PAYLOAD_LIMIT")

    def test_budget_timeout_and_time_limit(self):
        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'<rss><channel>'
                time.sleep(0.1)
                yield b'</channel></rss>'

        class BudgetCheck:
            def __call__(self):
                pass

            def remaining_seconds(self):
                return 0.05

        adapter = WeWorkRemotely(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream())),
            check=BudgetCheck(),
        )
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_TIME_LIMIT")
        self.assertEqual(adapter.health(), "SOURCE_TIME_LIMIT")

    def test_cancellation_during_stream_preserves_error_and_source_health(self):
        source_id = configure_source(self.store, "default", provider="wwr")
        cancelled = threading.Event()
        stream_started = threading.Event()
        release_stream = threading.Event()
        cancellation = OperationCancelled("synthetic stream cancel")
        errors = []

        class StalledStream(httpx.SyncByteStream):
            def __iter__(self):
                stream_started.set()
                release_stream.wait(timeout=2)
                yield b'<rss><channel></channel></rss>'

            def close(self):
                release_stream.set()

        def check():
            if cancelled.is_set():
                raise cancellation

        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=StalledStream()))

        def collect():
            try:
                collect_source(self.store, source_id, "fixture", check=check, transport=transport)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=collect)
        thread.start()
        self.assertTrue(stream_started.wait(timeout=1))
        cancelled.set()
        thread.join(timeout=0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [cancellation])

        with self.store.engine.connect() as conn:
            source = conn.execute(select(m.job_sources).where(m.job_sources.c.id == source_id)).mappings().one()
            self.assertEqual(source["health_status"], "NOT_CHECKED")
            self.assertEqual(source["consecutive_failures"], 0)
            self.assertIsNone(source["last_error"])


if __name__ == "__main__":
    unittest.main()
