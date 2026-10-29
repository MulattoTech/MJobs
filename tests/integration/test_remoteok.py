"""Integration and contract tests for the Remote OK global job-source adapter (MJ-REQ-017.S07)."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

import httpx
from workers.operations import OperationCancelled
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.contract import (
    CollectionStrategy, SearchBatch, SourceCapabilities, SourceCapability,
    SourceCompany, SourceItem,
)
from services.discovery.sources.remoteok import RemoteOK, SourceError
from services.discovery.sources.collect import configure_source, collect_source
from services.discovery.sources.registry import source_adapter
from tests.integration.test_spine import PROFILE

RAW_REMOTEOK_PAYLOAD = [
    {
        "legal": "Remote OK API is free for personal use.",
        "last_updated": "2026-10-06T18:00:00+00:00",
    },
    {
        "id": "2001",
        "slug": "remote-senior-backend-engineer-cloudscale-2001",
        "epoch": 1791205202,
        "date": "2026-10-05T13:00:02+00:00",
        "company": "CloudScale Inc",
        "company_logo": "https://remoteok.com/assets/cloudscale.png",
        "position": "Senior Backend Engineer",
        "tags": ["python", "golang", "kubernetes", "full time"],
        "description": "<p>Lead distributed systems development with Python and Kubernetes. &lt;script&gt;evil()&lt;/script&gt;</p>",
        "location": "Worldwide",
        "apply_url": "https://remoteok.com/remote-jobs/remote-senior-backend-engineer-cloudscale-2001",
        "salary_min": 140000,
        "salary_max": 180000,
        "url": "https://remoteok.com/remote-jobs/remote-senior-backend-engineer-cloudscale-2001",
    },
    {
        "id": "2002",
        "slug": "remote-frontend-developer-pixelcraft-2002",
        "epoch": 1791206000,
        "date": "2026-10-05T14:30:00+00:00",
        "company": "PixelCraft Studios",
        "company_logo": "",
        "position": "Frontend Developer",
        "tags": ["react", "typescript", "css", "part time"],
        "description": "<p>Build delightful user interfaces with React and TypeScript.</p>",
        "location": "USA Only",
        "apply_url": "https://remoteok.com/remote-jobs/remote-frontend-developer-pixelcraft-2002",
        "salary_min": 0,
        "salary_max": 0,
        "url": "https://remoteok.com/remote-jobs/remote-frontend-developer-pixelcraft-2002",
    },
]


class RemoteOKTests(unittest.TestCase):
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
        self.assertEqual(request.url.host, "remoteok.com")
        self.assertEqual(request.url.path, "/api")
        return httpx.Response(200, json=RAW_REMOTEOK_PAYLOAD)

    def test_search_contract_and_multi_company_attribution(self):
        adapter = RemoteOK(transport=httpx.MockTransport(self.transport))
        batch = adapter.search()
        self.assertEqual(batch.endpoint, "https://remoteok.com/api")
        # Metadata dict at index 0 is skipped; exactly 2 job items returned
        self.assertEqual(len(batch.items), 2)

        first = batch.items[0]
        self.assertEqual(first.raw["id"], "2001")
        self.assertEqual(first.company.name, "CloudScale Inc")
        self.assertEqual(first.company.identity_parts, ("remoteok", "cloudscale-inc"))
        self.assertEqual(first.company.domain, "https://remoteok.com")

        second = batch.items[1]
        self.assertEqual(second.raw["id"], "2002")
        self.assertEqual(second.company.name, "PixelCraft Studios")
        self.assertEqual(second.company.identity_parts, ("remoteok", "pixelcraft-studios"))

        self.assertIsNone(batch.source_company)
        self.assertEqual(adapter.health(), "HEALTHY")

    def test_normalize_maps_to_canonical_job_fields(self):
        adapter = RemoteOK()
        normalized = adapter.normalize(RAW_REMOTEOK_PAYLOAD[1])

        self.assertEqual(normalized["requisition"], "2001")
        self.assertEqual(normalized["title"], "Senior Backend Engineer")
        self.assertIn("Python", normalized["description"])
        self.assertNotIn("<p>", normalized["description"])
        self.assertNotIn("<script>", normalized["description"])
        self.assertEqual(normalized["remote_type"], "REMOTE")
        self.assertEqual(normalized["location"], "Worldwide")
        self.assertEqual(normalized["application_url"], "https://remoteok.com/remote-jobs/remote-senior-backend-engineer-cloudscale-2001")

        attrs = normalized["attributes"]
        self.assertEqual(attrs["provider"], "remoteok")
        self.assertEqual(attrs["company_name"], "CloudScale Inc")
        self.assertEqual(attrs["native_posting_id"], 2001)
        self.assertEqual(attrs["employment_type"], "FULL_TIME")
        self.assertIn("Python", normalized["skills"])
        self.assertEqual(attrs["salary_min"], 140000.0)
        self.assertEqual(attrs["salary_max"], 180000.0)
        self.assertEqual(attrs["salary_currency"], "USD")
        self.assertEqual(attrs["salary_period"], "YEAR")
        self.assertEqual(attrs["salary_source"], "SOURCE_PROVIDED")
        self.assertEqual(attrs["reported_posted_at"], "2026-10-05T13:00:02+00:00")

    def test_capabilities_and_unsupported_features(self):
        adapter = RemoteOK()
        caps = adapter.capabilities()
        self.assertEqual(caps.strategy, CollectionStrategy.PUBLIC_JSON)
        self.assertTrue(caps.supports(SourceCapability.DISCOVERY))
        self.assertTrue(caps.supports(SourceCapability.JOB_DETAIL))
        self.assertTrue(caps.supports(SourceCapability.SALARY))
        self.assertTrue(caps.supports(SourceCapability.REMOTE_METADATA))
        self.assertTrue(caps.supports(SourceCapability.APPLICATION_REDIRECT))
        self.assertFalse(caps.supports(SourceCapability.DIRECT_APPLICATION))
        self.assertFalse(caps.supports(SourceCapability.APPLICATION_STATUS))

    def test_fetch_job_by_id(self):
        adapter = RemoteOK(transport=httpx.MockTransport(self.transport))
        raw = adapter.fetch(2001)
        self.assertEqual(raw["id"], "2001")
        self.assertEqual(raw["position"], "Senior Backend Engineer")

        raw_str = adapter.fetch("2002")
        self.assertEqual(raw_str["id"], "2002")

        with self.assertRaises(SourceError):
            adapter.fetch(9999)

        with self.assertRaises(ValueError):
            adapter.fetch(0)

        with self.assertRaises(ValueError):
            adapter.fetch("   ")

    def test_search_filtering_and_limits(self):
        def filtered_transport(request):
            self.requests.append(request)
            params = dict(request.url.params)
            self.assertEqual(params.get("tag"), "python")
            return httpx.Response(200, json=RAW_REMOTEOK_PAYLOAD)

        adapter = RemoteOK(transport=httpx.MockTransport(filtered_transport))
        batch = adapter.search(tag="python", limit=1)
        self.assertEqual(len(batch.items), 1)
        self.assertEqual(batch.items[0].raw["id"], "2001")

        with self.assertRaises(ValueError):
            adapter.search(limit=-1)

    def test_salary_parsing_variations(self):
        adapter = RemoteOK()

        # Min only
        job_min_only = {
            "id": 3001,
            "position": "Data Scientist",
            "company": "DataCorp",
            "description": "Analyze big data.",
            "salary_min": 120000,
            "salary_max": 0,
        }
        norm = adapter.normalize(job_min_only)
        self.assertEqual(norm["attributes"]["salary_min"], 120000.0)
        self.assertIsNone(norm["attributes"]["salary_max"])
        self.assertEqual(norm["attributes"]["salary_period"], "YEAR")

        # Max only
        job_max_only = {
            "id": 3002,
            "position": "Junior Analyst",
            "company": "DataCorp",
            "description": "Analyze reports.",
            "salary_min": 0,
            "salary_max": 80000,
        }
        norm = adapter.normalize(job_max_only)
        self.assertIsNone(norm["attributes"]["salary_min"])
        self.assertEqual(norm["attributes"]["salary_max"], 80000.0)
        self.assertEqual(norm["attributes"]["salary_period"], "YEAR")

        # Fallback to description
        job_desc_sal = {
            "id": 3003,
            "position": "DevOps Engineer",
            "company": "DataCorp",
            "description": "Salary: $95,000 - $115,000 / year. Cloud infrastructure.",
            "salary_min": 0,
            "salary_max": 0,
        }
        norm = adapter.normalize(job_desc_sal)
        self.assertEqual(norm["attributes"]["salary_min"], 95000.0)
        self.assertEqual(norm["attributes"]["salary_max"], 115000.0)
        self.assertEqual(norm["attributes"]["salary_period"], "YEAR")

    def test_error_handling_and_health_states(self):
        def error_transport(request):
            if "tag=status-500" in str(request.url):
                return httpx.Response(500, text="Server Error")
            if "tag=status-429" in str(request.url):
                return httpx.Response(429, headers={"retry-after": "45"}, text="Rate limited")
            if "tag=invalid-json" in str(request.url):
                return httpx.Response(200, text="<html>cloudflare challenge</html>")
            if "tag=dict-not-list" in str(request.url):
                return httpx.Response(200, json={"error": "not a list"})
            raise httpx.ConnectTimeout("connection timed out")

        # 500 error
        adapter = RemoteOK(transport=httpx.MockTransport(error_transport))
        with self.assertRaises(SourceError) as cm:
            adapter.search(tag="status-500")
        self.assertEqual(cm.exception.code, "SOURCE_SERVER_ERROR")
        self.assertEqual(adapter.health(), "SOURCE_SERVER_ERROR")

        # 429 error
        with self.assertRaises(SourceError) as cm:
            adapter.search(tag="status-429")
        self.assertEqual(cm.exception.code, "SOURCE_RATE_LIMITED")
        self.assertEqual(cm.exception.retry_seconds, 45)
        self.assertEqual(adapter.health(), "SOURCE_RATE_LIMITED")

        # Invalid json (html)
        with self.assertRaises(SourceError) as cm:
            adapter.search(tag="invalid-json")
        self.assertEqual(cm.exception.code, "SOURCE_PAYLOAD_INVALID")

        # Dict instead of expected list
        with self.assertRaises(SourceError) as cm:
            adapter.search(tag="dict-not-list")
        self.assertEqual(cm.exception.code, "SOURCE_PAYLOAD_INVALID")

        # Timeout
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_TIMEOUT")
        self.assertEqual(adapter.health(), "SOURCE_TIMEOUT")

    def test_registry_integration(self):
        policy = {"provider": "remoteok", "tenant": "default", "request_timeout": 15, "rate_limit": 30, "concurrency": 2}
        adapter = source_adapter(policy)
        self.assertIsInstance(adapter, RemoteOK)
        self.assertEqual(adapter.request_timeout, 15)
        self.assertEqual(adapter.rate_limit, 30)
        self.assertEqual(adapter.concurrency, 2)

        # Alias remote_ok
        policy_alias = {"provider": "remote_ok", "tenant": "default"}
        adapter_alias = source_adapter(policy_alias)
        self.assertIsInstance(adapter_alias, RemoteOK)

    def test_collect_source_persists_multi_company_jobs(self):
        source_id = configure_source(self.store, "default", provider="remoteok", poll_interval=300)
        self.assertTrue(source_id)

        collect_source(self.store, source_id, "fixture", transport=httpx.MockTransport(self.transport))

        with self.store.engine.connect() as conn:
            companies = conn.execute(select(m.companies)).mappings().all()
            company_names = {c["name"] for c in companies}
            self.assertIn("CloudScale Inc", company_names)
            self.assertIn("PixelCraft Studios", company_names)

            postings = conn.execute(select(m.postings)).mappings().all()
            self.assertEqual(len(postings), 2)

            snapshots = conn.execute(select(m.posting_snapshots)).mappings().all()
            self.assertEqual(len(snapshots), 2)

            source_row = conn.execute(select(m.job_sources).where(m.job_sources.c.id == source_id)).mappings().one()
            self.assertEqual(source_row["health_status"], "HEALTHY")
            self.assertEqual(source_row["provider"], "remoteok")
            self.assertEqual(source_row["strategy"], "PUBLIC_JSON")
            self.assertEqual(source_row["jobs_seen"], 2)

    def test_live_bounded_search_read_only(self):
        """Bounded live read-only verification against Remote OK public API when enabled."""
        import os
        if not os.environ.get("MJOBS_ENABLE_LIVE_TESTS"):
            return
        adapter = RemoteOK(request_timeout=15)
        batch = adapter.search(tag="engineer", limit=3)
        self.assertGreater(len(batch.items), 0)
        self.assertLessEqual(len(batch.items), 3)
        for item in batch.items:
            norm = adapter.normalize(item.raw)
            self.assertTrue(norm["title"])
            self.assertTrue(norm["requisition"])
            self.assertEqual(norm["remote_type"], "REMOTE")
            self.assertEqual(norm["attributes"]["provider"], "remoteok")
            self.assertEqual(norm["attributes"]["lifecycle_claim"], "ACTIVE")

    def test_payload_limit_rejected(self):
        class LargeStream(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(80):
                    yield b"x" * 65536

        adapter = RemoteOK(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=LargeStream())))
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_PAYLOAD_LIMIT")
        self.assertEqual(adapter.health(), "SOURCE_PAYLOAD_LIMIT")

    def test_budget_timeout_and_time_limit(self):
        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'[{"legal": "notice"}, '
                time.sleep(0.1)
                yield b']'

        class BudgetCheck:
            def __call__(self):
                pass

            def remaining_seconds(self):
                return 0.05

        adapter = RemoteOK(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream())),
            check=BudgetCheck(),
        )
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_TIME_LIMIT")
        self.assertEqual(adapter.health(), "SOURCE_TIME_LIMIT")

    def test_cancellation_during_stream_preserves_error_and_source_health(self):
        source_id = configure_source(self.store, "default", provider="remoteok")
        cancelled = threading.Event()
        worker_ready = threading.Event()
        release_worker = threading.Event()
        stream_started = threading.Event()
        release_stream = threading.Event()
        cancellation = OperationCancelled("synthetic stream cancel")
        errors = []

        class StalledStream(httpx.SyncByteStream):
            def __iter__(self):
                stream_started.set()
                release_stream.wait(timeout=2)
                yield b'[{"legal": "notice"}]'

            def close(self):
                release_stream.set()

        def check():
            if cancelled.is_set():
                raise cancellation

        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=StalledStream()))

        def collect():
            worker_ready.set()
            release_worker.wait()
            try:
                collect_source(self.store, source_id, "fixture", check=check, transport=transport)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=collect)
        thread.start()
        self.assertTrue(worker_ready.wait(timeout=3), "collector worker did not start")
        release_worker.set()
        self.assertTrue(stream_started.wait(timeout=3), "collector did not enter the response stream")
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

