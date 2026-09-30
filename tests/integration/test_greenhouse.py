"""First-party public source contracts; no employer submission endpoints."""
from pathlib import Path
import tempfile
import unittest

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.greenhouse import Greenhouse
from services.discovery.sources.collect import configure_source, collect_source
from tests.integration.test_spine import PROFILE

RAW = {"id": 1234, "internal_job_id": 5678, "title": "Python Engineer", "location": {"name": "Remote - United States"},
       "content": "&lt;p&gt;Build services with Python and SQL. &lt;script&gt;attack()&lt;/script&gt;&lt;/p&gt;",
       "updated_at": "2026-09-29T12:00:00Z", "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1234"}


class GreenhouseTests(unittest.TestCase):
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
        self.assertEqual(request.url.host, "boards-api.greenhouse.io")
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [RAW, dict(RAW, id=1235, title="Platform Engineer")], "meta": {"total": 2}})
        return httpx.Response(200, json={"name": "Example Public Employer"})

    def test_collection_preserves_provenance_and_labels_live_matching(self):
        source = configure_source(self.store, "example")
        result = collect_source(self.store, source, "fixture", transport=httpx.MockTransport(self.transport))
        self.assertEqual(result["jobs_seen"], 2)
        self.assertEqual(self.store.count("canonical_jobs"), 2)
        self.assertEqual(self.store.count("posting_snapshots"), 2)
        self.assertEqual(self.store.count("company_job_sources"), 1)
        with self.store.engine.connect() as connection:
            snapshots = list(connection.execute(select(m.posting_snapshots)).mappings())
            match = connection.execute(select(m.matches)).mappings().first()
        self.assertEqual(snapshots[0]["raw"], RAW)
        self.assertEqual(len(snapshots[0]["sha256"]), 64)
        self.assertEqual(match["components"]["method"], "description_skill_mentions")
        self.assertTrue(all("jobs" in str(request.url) or str(request.url).endswith("/example") for request in self.requests))
        self.assertFalse(Greenhouse("example").capabilities()["direct_application"])

    def test_cooldown_prevents_repeated_requests_and_failure_does_not_erase_jobs(self):
        source = configure_source(self.store, "example")
        transport = httpx.MockTransport(self.transport)
        collect_source(self.store, source, "fixture", transport=transport)
        with self.assertRaisesRegex(ValueError, "SOURCE_COOLDOWN"):
            collect_source(self.store, source, "fixture", transport=transport)
        self.assertEqual(len(self.requests), 2)
        other = configure_source(self.store, "failing")
        with self.assertRaisesRegex(ValueError, "SOURCE_HTTP_503"):
            collect_source(self.store, other, "fixture", transport=httpx.MockTransport(lambda _: httpx.Response(503)))
        self.assertEqual(self.store.count("canonical_jobs"), 2)

    def test_tenant_redirect_and_large_payload_are_rejected(self):
        for tenant in ("../secret", "https://outside.test", "a?token=value", "a@host"):
            with self.assertRaises(ValueError):
                Greenhouse(tenant)
        with self.assertRaisesRegex(ValueError, "SOURCE_REDIRECT_BLOCKED"):
            Greenhouse("example", transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": "http://127.0.0.1"}))).collect()
        with self.assertRaisesRegex(ValueError, "SOURCE_PAYLOAD_LIMIT"):
            Greenhouse("example", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 5_000_001, headers={"content-type": "application/json"}))).collect()

    def test_rate_limit_persists_provider_backoff(self):
        source = configure_source(self.store, "example")
        with self.assertRaisesRegex(ValueError, "SOURCE_RATE_LIMITED"):
            collect_source(self.store, source, "fixture", transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"retry-after": "600"})))
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.job_sources)).mappings().one()
        self.assertEqual(row["health_status"], "RATE_LIMITED")
        self.assertIsNotNone(row["backoff_until"])

    def test_missing_fields_are_normalized_without_inventing_data(self):
        job = Greenhouse("example").normalize({"id": 8, "title": "Role"})
        self.assertEqual(job["location"], "Unknown")
        self.assertEqual(job["remote_type"], "UNKNOWN")
        self.assertEqual(job["description"], "")
        self.assertIsNone(job["attributes"]["salary"])
        self.assertEqual(job["application_url"], "")

    def test_malformed_rows_and_untrusted_routes_do_not_invent_evidence(self):
        adapter = Greenhouse("example")
        with self.assertRaisesRegex(ValueError, "SOURCE_INVALID_JOB_LOCATION"):
            adapter.normalize(dict(RAW, location="Remote"))
        self.assertEqual(adapter.normalize(dict(RAW, absolute_url="https://outside.test/apply"))["application_url"], "")
        source = configure_source(self.store, "example")
        def response(request):
            return httpx.Response(200, json={"jobs": [RAW, dict(RAW, id=9, location=[])]}) if request.url.path.endswith("/jobs") else httpx.Response(200, json={"name": "Example"})
        # A nonempty malformed location is rejected while the valid row persists.
        def malformed(request):
            return httpx.Response(200, json={"jobs": [RAW, dict(RAW, id=9, location=["Remote"])]}) if request.url.path.endswith("/jobs") else response(request)
        result = collect_source(self.store, source, "fixture", transport=httpx.MockTransport(malformed))
        self.assertEqual((result["jobs_seen"], result["rejected"]), (1, 1))

    def test_adapter_health_and_detail_validation(self):
        adapter = Greenhouse("example", transport=httpx.MockTransport(lambda request: httpx.Response(429) if request.url.path.endswith("/jobs") else httpx.Response(200, json={"name": "Example"})))
        with self.assertRaises(ValueError):
            adapter.collect()
        self.assertEqual(adapter.health(), "SOURCE_RATE_LIMITED")
        with self.assertRaisesRegex(ValueError, "SOURCE_INVALID_JOB"):
            Greenhouse("example", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"not": "job"}))).fetch(1234)

    def test_extreme_provider_backoff_cannot_be_shortened_by_reconfiguration(self):
        source = configure_source(self.store, "example")
        with self.assertRaisesRegex(ValueError, "SOURCE_RATE_LIMITED"):
            collect_source(self.store, source, "fixture", transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"retry-after": "100000000000000000000"})))
        configure_source(self.store, "example", enabled=True)
        with self.assertRaisesRegex(ValueError, "SOURCE_BACKOFF"):
            collect_source(self.store, source, "fixture", transport=httpx.MockTransport(lambda _: self.fail("No request during provider backoff")))
