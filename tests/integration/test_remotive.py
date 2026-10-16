"""Integration and contract tests for the Remotive global job-source adapter (MJ-REQ-017.S08)."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

import httpx
from workers.operations import OperationCancelled
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.contract import (
    CollectionStrategy, SearchBatch, SourceCapabilities, SourceCapability,
    SourceCompany, SourceItem,
)
from services.discovery.sources.remotive import Remotive, SourceError
from services.discovery.sources.collect import configure_source, collect_source
from services.discovery.sources.registry import source_adapter
from tests.integration.test_spine import PROFILE

RAW_REMOTIVE_PAYLOAD = {
    "0-legal-notice": "Remotive API is free for personal use.",
    "job-count": 2,
    "jobs": [
        {
            "id": 1001,
            "url": "https://remotive.com/remote-jobs/software-dev/senior-python-engineer-1001",
            "title": "Senior Python Engineer",
            "company_name": "Acme Cloud",
            "company_logo": "https://remotive.com/job/1001/logo",
            "category": "Software Development",
            "tags": ["python", "sql", "api", "backend"],
            "job_type": "full_time",
            "publication_date": "2026-10-01T12:00:00",
            "candidate_required_location": "USA Only",
            "salary": "$130,000 - $160,000",
            "description": "<p>Build high-scale services using Python, SQL, and REST APIs. &lt;script&gt;alert(1)&lt;/script&gt;</p>",
        },
        {
            "id": 1002,
            "url": "https://remotive.com/remote-jobs/software-dev/frontend-engineer-1002",
            "title": "Frontend Engineer",
            "company_name": "Globex Systems",
            "company_logo": "",
            "category": "Software Development",
            "tags": ["react", "typescript"],
            "job_type": "full_time",
            "publication_date": "2026-10-02T15:30:00",
            "candidate_required_location": "Worldwide",
            "salary": "",
            "description": "<p>Develop reactive user interfaces using TypeScript.</p>",
        },
    ],
}


class RemotiveTests(unittest.TestCase):
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
        self.assertEqual(request.url.host, "remotive.com")
        self.assertEqual(request.url.path, "/api/remote-jobs")
        return httpx.Response(200, json=RAW_REMOTIVE_PAYLOAD)

    def test_search_contract_and_multi_company_attribution(self):
        adapter = Remotive(transport=httpx.MockTransport(self.transport))
        batch = adapter.search()
        self.assertEqual(batch.endpoint, "https://remotive.com/api/remote-jobs")
        self.assertEqual(len(batch.items), 2)

        first = batch.items[0]
        self.assertEqual(first.raw["id"], 1001)
        self.assertEqual(first.company.name, "Acme Cloud")
        self.assertEqual(first.company.identity_parts, ("remotive", "acme-cloud"))

        second = batch.items[1]
        self.assertEqual(second.raw["id"], 1002)
        self.assertEqual(second.company.name, "Globex Systems")
        self.assertEqual(second.company.identity_parts, ("remotive", "globex-systems"))

        self.assertIsNone(batch.source_company)
        self.assertEqual(adapter.health(), "HEALTHY")

    def test_normalize_maps_to_canonical_job_fields(self):
        adapter = Remotive()
        normalized = adapter.normalize(RAW_REMOTIVE_PAYLOAD["jobs"][0])

        self.assertEqual(normalized["requisition"], "1001")
        self.assertEqual(normalized["title"], "Senior Python Engineer")
        self.assertIn("Python", normalized["description"])
        self.assertNotIn("<p>", normalized["description"])
        self.assertNotIn("<script>", normalized["description"])
        self.assertEqual(normalized["remote_type"], "REMOTE")
        self.assertEqual(normalized["location"], "USA Only")
        self.assertEqual(normalized["application_url"], "https://remotive.com/remote-jobs/software-dev/senior-python-engineer-1001")

        attrs = normalized["attributes"]
        self.assertEqual(attrs["provider"], "remotive")
        self.assertEqual(attrs["company_name"], "Acme Cloud")
        self.assertEqual(attrs["native_posting_id"], 1001)
        self.assertEqual(attrs["employment_type"], "FULL_TIME")
        self.assertIn("Python", normalized["skills"])

    def test_capabilities_and_unsupported_features(self):
        adapter = Remotive()
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
        adapter = Remotive(transport=httpx.MockTransport(self.transport))
        raw = adapter.fetch(1001)
        self.assertEqual(raw["id"], 1001)
        self.assertEqual(raw["title"], "Senior Python Engineer")

        with self.assertRaises(SourceError):
            adapter.fetch(9999)

    def test_error_handling_and_health_states(self):
        def error_transport(request):
            if "status-500" in str(request.url):
                return httpx.Response(500, text="Server Error")
            if "status-429" in str(request.url):
                return httpx.Response(429, headers={"retry-after": "45"}, text="Rate limited")
            if "invalid-json" in str(request.url):
                return httpx.Response(200, text="not json")
            raise httpx.ConnectTimeout("connection timed out")

        # 500 error
        adapter = Remotive(transport=httpx.MockTransport(error_transport))
        with self.assertRaises(SourceError) as cm:
            adapter.search(search_term="status-500")
        self.assertEqual(cm.exception.code, "SOURCE_SERVER_ERROR")
        self.assertEqual(adapter.health(), "SOURCE_SERVER_ERROR")

        # 429 error
        with self.assertRaises(SourceError) as cm:
            adapter.search(search_term="status-429")
        self.assertEqual(cm.exception.code, "SOURCE_RATE_LIMITED")
        self.assertEqual(cm.exception.retry_seconds, 45)
        self.assertEqual(adapter.health(), "SOURCE_RATE_LIMITED")

        # Invalid json
        with self.assertRaises(SourceError) as cm:
            adapter.search(search_term="invalid-json")
        self.assertEqual(cm.exception.code, "SOURCE_PAYLOAD_INVALID")

        # Timeout
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_TIMEOUT")
        self.assertEqual(adapter.health(), "SOURCE_TIMEOUT")

    def test_registry_integration(self):
        policy = {"provider": "remotive", "tenant": "default", "request_timeout": 15, "rate_limit": 30, "concurrency": 2}
        adapter = source_adapter(policy)
        self.assertIsInstance(adapter, Remotive)
        self.assertEqual(adapter.request_timeout, 15)
        self.assertEqual(adapter.rate_limit, 30)
        self.assertEqual(adapter.concurrency, 2)

    def test_collect_source_persists_multi_company_jobs(self):
        source_id = configure_source(self.store, "default", provider="remotive", poll_interval=300)
        self.assertTrue(source_id)

        collect_source(self.store, source_id, "fixture", transport=httpx.MockTransport(self.transport))

        with self.store.engine.connect() as conn:
            companies = conn.execute(select(m.companies)).mappings().all()
            company_names = {c["name"] for c in companies}
            self.assertIn("Acme Cloud", company_names)
            self.assertIn("Globex Systems", company_names)

            postings = conn.execute(select(m.postings)).mappings().all()
            self.assertEqual(len(postings), 2)

            snapshots = conn.execute(select(m.posting_snapshots)).mappings().all()
            self.assertEqual(len(snapshots), 2)

            source_row = conn.execute(select(m.job_sources).where(m.job_sources.c.id == source_id)).mappings().one()
            self.assertEqual(source_row["health_status"], "HEALTHY")
            self.assertEqual(source_row["provider"], "remotive")

    def test_link_boundary_survives_collection_reopen_and_api_readback(self):
        urls = (
            "https://remotive.com.attacker.invalid/jobs/1",
            "https://notremotive.com/jobs/1",
            "https://@remotive.com/jobs/1",
            "https:///jobs/1",
            "https://[broken/jobs/1",
            "http://remotive.com/jobs/1",
            RAW_REMOTIVE_PAYLOAD["jobs"][0]["url"],
        )
        payload = {"jobs": [dict(RAW_REMOTIVE_PAYLOAD["jobs"][0],
                                id=9000 + index, title=f"Safety Engineer {index}", url=url)
                            for index, url in enumerate(urls)]}
        requests = []

        def transport(request):
            requests.append(str(request.url))
            self.assertEqual(str(request.url), "https://remotive.com/api/remote-jobs")
            return httpx.Response(200, json=payload)

        source_id = configure_source(self.store, "default", provider="remotive")
        result = collect_source(self.store, source_id, "fixture", transport=httpx.MockTransport(transport))
        self.assertEqual(result["jobs_seen"], len(urls))
        self.assertFalse(result["direct_application"])
        self.assertFalse(result["rejected"])
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        with self.store.engine.connect() as connection:
            jobs = connection.execute(select(m.jobs)).mappings().all()
            postings = connection.execute(select(m.postings)).mappings().all()
            snapshots = connection.execute(select(m.posting_snapshots)).mappings().all()
            routes = connection.execute(select(m.routes)).mappings().all()
            tenants = connection.execute(select(m.company_ats_tenants)).mappings().all()
        self.assertEqual(len(jobs), len(urls))
        self.assertEqual(len(snapshots), len(urls))
        self.assertEqual(tenants, [])
        self.assertEqual(self.store.count("applications"), 0)
        for posting in postings:
            index = int(posting["raw"]["id"]) - 9000
            self.assertEqual(posting["raw"]["url"], urls[index])
            self.assertEqual(posting["application_url"], urls[index] if index == len(urls) - 1 else "")
        self.assertEqual({route["url"] for route in routes}, {"", urls[-1]})
        with TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                  "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
            for _ in range(2):
                for job in jobs:
                    index = int(job["requisition"]) - 9000
                    response = client.get(f"/jobs/{job['id']}")
                    self.assertEqual(response.status_code, 200, response.text)
                    detail = response.json()
                    self.assertEqual(detail["application_url"] or "", urls[index] if index == len(urls) - 1 else "")
                    self.assertEqual(detail["attributes"]["submission_capability"], "MANUAL_ONLY")
                    self.assertEqual(detail["attributes"]["application_url_status"],
                                     "PROVIDER_SUPPLIED" if index == len(urls) - 1 else "UNAVAILABLE")
        self.assertEqual(requests, ["https://remotive.com/api/remote-jobs"])

    def test_payload_limit_rejected(self):
        class LargeStream(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(80):
                    yield b"x" * 65536

        adapter = Remotive(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=LargeStream())))
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_PAYLOAD_LIMIT")
        self.assertEqual(adapter.health(), "SOURCE_PAYLOAD_LIMIT")

    def test_budget_timeout_and_time_limit(self):
        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'{"jobs": ['
                time.sleep(0.1)
                yield b']}'

        class BudgetCheck:
            def __call__(self):
                pass

            def remaining_seconds(self):
                return 0.05

        adapter = Remotive(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream())),
            check=BudgetCheck(),
        )
        with self.assertRaises(SourceError) as cm:
            adapter.search()
        self.assertEqual(cm.exception.code, "SOURCE_TIME_LIMIT")
        self.assertEqual(adapter.health(), "SOURCE_TIME_LIMIT")

    def test_cancellation_during_stream_preserves_error_and_source_health(self):
        source_id = configure_source(self.store, "default", provider="remotive")
        cancelled = threading.Event()
        stream_started = threading.Event()
        release_stream = threading.Event()
        cancellation = OperationCancelled("synthetic stream cancel")
        errors = []

        class StalledStream(httpx.SyncByteStream):
            def __iter__(self):
                stream_started.set()
                release_stream.wait(timeout=2)
                yield b'{"jobs": []}'

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
