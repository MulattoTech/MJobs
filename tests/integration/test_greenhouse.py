"""First-party public source contracts; no employer submission endpoints."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import importlib
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity, now, upsert
from services.company_discovery.domains import record_company_domain
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.sources.contract import (
    CollectionStrategy, SearchBatch, SourceCapabilities, SourceCapability,
    SourceCompany, SourceItem,
)
from services.discovery.sources.greenhouse import Greenhouse
from services.discovery.sources.errors import SourceError
from services.discovery.sources.collect import configure_source, collect_source, persist_posting
from services.discovery.sources.registry import source_adapter
from tests.integration.test_spine import PROFILE
from workers.operations import OperationCancelled, OperationTimedOut

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

    def bind_collector(self, store, source_id, tenant="example"):
        company_id = f"company-{tenant}"
        domain = f"{tenant}.example"
        with store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company_id, "name": f"{tenant} employer", "domain": "",
            })
        record_company_domain(
            store, company_id, domain, "FIRST_PARTY_JOB_LINK", ["synthetic verified job"],
        )
        company_source = upsert_company_job_source(
            store,
            company_id=company_id,
            provider="greenhouse",
            tenant_identifier=tenant,
            career_url=f"https://{domain}/careers",
            public_api_url=f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs",
            collection_strategy="API",
            confidence=0.99,
            discovered_automatically=True,
        )
        with store.engine.begin() as connection:
            connection.execute(update(m.company_sources).where(
                m.company_sources.c.id == company_source["id"]
            ).values(collector_source_id=source_id))
        return company_source

    def transport(self, request):
        self.requests.append(request)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.host, "boards-api.greenhouse.io")
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [RAW, dict(RAW, id=1235, title="Platform Engineer")], "meta": {"total": 2}})
        return httpx.Response(200, json={"name": "Example Public Employer"})

    def test_search_contract_keeps_raw_jobs_and_company_attribution(self):
        adapter = Greenhouse("example", transport=httpx.MockTransport(self.transport))
        self.assertTrue(callable(getattr(adapter, "search", None)))
        batch = adapter.search()
        self.assertEqual(batch.endpoint, "https://boards-api.greenhouse.io/v1/boards/example/jobs")
        self.assertEqual(len(batch.items), 2)
        self.assertEqual(batch.items[0].raw, RAW)
        self.assertEqual(batch.items[0].company.identity_parts, ("greenhouse", "example"))
        self.assertEqual(batch.items[0].company.name, "Example Public Employer")
        self.assertEqual(batch.items[0].company.domain, "https://job-boards.greenhouse.io/example")
        self.assertEqual(adapter.capabilities().strategy, CollectionStrategy.API)
        self.assertFalse(adapter.capabilities().supports(SourceCapability.DIRECT_APPLICATION))

    def test_http_request_timeout_is_capped_by_remaining_worker_budget(self):
        observed_timeouts = []
        check = lambda: None
        check.remaining_seconds = lambda: 0.125
        real_client = httpx.Client

        def capture_client(*args, **kwargs):
            observed_timeouts.append(httpx.Timeout(kwargs["timeout"]))
            return real_client(*args, **kwargs)

        with patch("services.discovery.sources.greenhouse.httpx.Client", side_effect=capture_client):
            Greenhouse("example", transport=httpx.MockTransport(self.transport), check=check,
                       request_timeout=10, concurrency=2).search()

        self.assertEqual(len(observed_timeouts), 2)
        for timeout in observed_timeouts:
            self.assertLessEqual(timeout.connect, 0.125)
            self.assertLessEqual(timeout.read, 0.125)

    def test_response_body_deadline_is_capped_by_remaining_worker_budget(self):
        class SlowBody(httpx.SyncByteStream):
            def __iter__(self):
                time.sleep(0.05)
                yield b"{}"

        budgets = iter((10, 0.01))
        check = lambda: None
        check.remaining_seconds = lambda: next(budgets)
        transport = httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "application/json"}, stream=SlowBody()))
        adapter = Greenhouse("example", transport=transport, check=check, request_timeout=10)

        with self.assertRaisesRegex(ValueError, "SOURCE_TIME_LIMIT"):
            adapter._get("/jobs")

    def test_http_response_wait_observes_worker_cancellation_and_preserves_its_error(self):
        cancelled = threading.Event()
        body_started = threading.Event()
        release_body = threading.Event()
        cancellation = OperationCancelled("synthetic local cancel")
        errors = []

        class StalledBody(httpx.SyncByteStream):
            def __iter__(self):
                body_started.set()
                release_body.wait(timeout=2)
                yield b'{"name":"Example Public Employer"}'

            def close(self):
                release_body.set()

        def check():
            if cancelled.is_set():
                raise cancellation

        adapter = Greenhouse("example", transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "application/json"}, stream=StalledBody())), check=check)

        def request():
            try:
                adapter._request("")
            except Exception as error:
                errors.append(error)

        try:
            thread = threading.Thread(target=request)
            thread.start()
            self.assertTrue(body_started.wait(timeout=1), "HTTP body read did not start")
            cancelled.set()
            thread.join(timeout=0.3)
            self.assertFalse(thread.is_alive(), "HTTP body read ignored worker cancellation")
            self.assertEqual(errors, [cancellation])
        finally:
            release_body.set()

    def test_cancellation_during_source_body_read_preserves_health_and_import_history(self):
        source_id = configure_source(self.store, "cancelled-stream", concurrency=1,
                                     rate_limit=600, jitter_seconds=0)
        cancelled = threading.Event()
        body_started = threading.Event()
        release_body = threading.Event()
        cancellation = OperationCancelled("synthetic local cancel")
        errors = []

        class StalledBody(httpx.SyncByteStream):
            def __iter__(self):
                body_started.set()
                release_body.wait(timeout=2)
                yield b'{"name":"Example Public Employer"}'

            def close(self):
                release_body.set()

        def check():
            if cancelled.is_set():
                raise cancellation

        transport = httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "application/json"}, stream=StalledBody()))

        def collect():
            try:
                collect_source(self.store, source_id, "fixture", check=check, transport=transport)
            except Exception as error:
                errors.append(error)

        try:
            thread = threading.Thread(target=collect)
            thread.start()
            self.assertTrue(body_started.wait(timeout=1), "source body read did not start")
            cancelled.set()
            thread.join(timeout=0.5)
            self.assertFalse(thread.is_alive(), "source body read ignored worker cancellation")
            self.assertEqual(errors, [cancellation])
        finally:
            release_body.set()

        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one())
            observations = list(connection.execute(select(m.source_quality_observations).where(
                m.source_quality_observations.c.configured_source_id == source_id)).mappings())
            jobs = list(connection.execute(select(m.jobs.c.id)).mappings())
        self.assertEqual(source["health_status"], "NOT_CHECKED")
        self.assertEqual(source["consecutive_failures"], 0)
        self.assertIsNone(source["last_error"])
        self.assertEqual(observations, [])
        self.assertEqual(jobs, [])

    def test_per_source_concurrency_bounds_parallel_read_requests(self):
        def maximum_in_flight(concurrency):
            active = 0
            maximum = 0
            starts = []
            guard = threading.Lock()

            def slow_transport(request):
                nonlocal active, maximum
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                    starts.append(time.monotonic())
                try:
                    # Slow synthetic reads overlap only when the per-source
                    # limit allows it; no external request is made.
                    time.sleep(0.25)
                    if request.url.path.endswith("/jobs"):
                        return httpx.Response(200, json={"jobs": [RAW]})
                    return httpx.Response(200, json={"name": "Example Public Employer"})
                finally:
                    with guard:
                        active -= 1

            real_client = httpx.Client

            def synthetic_client(*args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(slow_transport)
                return real_client(*args, **kwargs)

            with patch("services.discovery.sources.greenhouse.httpx.Client",
                       side_effect=synthetic_client):
                adapter = Greenhouse("example", concurrency=concurrency, rate_limit=600)
                batch = adapter.search()
            self.assertEqual(len(batch.items), 1)
            return maximum, sorted(starts)

        sequential_maximum, sequential_starts = maximum_in_flight(1)
        self.assertEqual(sequential_maximum, 1)
        self.assertGreaterEqual(sequential_starts[1] - sequential_starts[0], 0.075)
        parallel_maximum, parallel_starts = maximum_in_flight(2)
        self.assertEqual(parallel_maximum, 2)
        self.assertGreaterEqual(parallel_starts[1] - parallel_starts[0], 0.075)
        # A configured cap above the number of independent requests is still
        # bounded by actual work; it must not create idle worker threads.
        self.assertEqual(maximum_in_flight(8)[0], 2)

    def test_persisted_source_concurrency_controls_collection_requests(self):
        source_id = configure_source(self.store, "configured-concurrency", concurrency=2,
                                     rate_limit=600, jitter_seconds=0)
        active = 0
        maximum = 0
        guard = threading.Lock()

        def slow_transport(request):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.25)
                if request.url.path.endswith("/jobs"):
                    return httpx.Response(200, json={"jobs": [dict(
                        RAW,
                        absolute_url=(
                            "https://job-boards.greenhouse.io/"
                            "configured-concurrency/jobs/1234"
                        ),
                    )]})
                return httpx.Response(200, json={"name": "Configured Concurrency Employer"})
            finally:
                with guard:
                    active -= 1

        real_client = httpx.Client

        def synthetic_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(slow_transport)
            return real_client(*args, **kwargs)

        with patch("services.discovery.sources.greenhouse.httpx.Client",
                   side_effect=synthetic_client):
            result = collect_source(self.store, source_id, "fixture")
        self.assertEqual(result["jobs_seen"], 1)
        self.assertEqual(maximum, 2)

    def test_parallel_fetch_failure_records_history_without_partial_jobs(self):
        source_id = configure_source(self.store, "parallel-failure", concurrency=2,
                                     rate_limit=600, jitter_seconds=0)

        def mixed_transport(request):
            if request.url.path.endswith("/jobs"):
                return httpx.Response(429, headers={"retry-after": "60"})
            time.sleep(0.1)
            return httpx.Response(200, json={"name": "Example Public Employer"})

        real_client = httpx.Client

        def synthetic_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(mixed_transport)
            return real_client(*args, **kwargs)

        with patch("services.discovery.sources.greenhouse.httpx.Client",
                   side_effect=synthetic_client):
            with self.assertRaisesRegex(ValueError, "SOURCE_RATE_LIMITED"):
                collect_source(self.store, source_id, "fixture")
        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one())
            observations = list(connection.execute(select(m.source_quality_observations).where(
                m.source_quality_observations.c.configured_source_id == source_id)).mappings())
            job_count = connection.execute(select(m.jobs.c.id)).fetchall()
        self.assertEqual(source["health_status"], "RATE_LIMITED")
        self.assertEqual(source["last_error"], "SOURCE_RATE_LIMITED")
        self.assertEqual(len(observations), 1)
        self.assertFalse(observations[0]["succeeded"])
        self.assertEqual(job_count, [])

    def test_local_cancellation_outweighs_concurrent_network_failure(self):
        source_id = configure_source(self.store, "parallel-cancel", concurrency=2,
                                     rate_limit=600, jitter_seconds=0)
        cancelled = threading.Event()

        def check():
            if cancelled.is_set():
                raise OperationCancelled("synthetic local cancel")
            if threading.current_thread().name.endswith("_1"):
                cancelled.set()
                raise OperationCancelled("synthetic local cancel")

        def transport(request):
            self.assertFalse(request.url.path.endswith("/jobs"))
            self.assertTrue(cancelled.wait(timeout=2))
            raise httpx.ConnectError("synthetic sibling failure", request=request)

        real_client = httpx.Client

        def synthetic_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(transport)
            return real_client(*args, **kwargs)

        with patch("services.discovery.sources.greenhouse.httpx.Client",
                   side_effect=synthetic_client):
            with self.assertRaises(OperationCancelled):
                collect_source(self.store, source_id, "fixture", check=check)
        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one())
            observations = list(connection.execute(select(m.source_quality_observations).where(
                m.source_quality_observations.c.configured_source_id == source_id)).mappings())
        self.assertEqual(source["health_status"], "NOT_CHECKED")
        self.assertEqual(source["consecutive_failures"], 0)
        self.assertIsNone(source["last_error"])
        self.assertEqual(observations, [])

    def test_successful_parallel_request_does_not_hide_provider_failure(self):
        def mixed_transport(request):
            if request.url.path.endswith("/jobs"):
                return httpx.Response(429, headers={"retry-after": "60"})
            time.sleep(0.1)
            return httpx.Response(200, json={"name": "Example Public Employer"})

        real_client = httpx.Client

        def synthetic_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(mixed_transport)
            return real_client(*args, **kwargs)

        with patch("services.discovery.sources.greenhouse.httpx.Client",
                   side_effect=synthetic_client):
            adapter = Greenhouse("example", concurrency=2, rate_limit=600)
            with self.assertRaisesRegex(ValueError, "SOURCE_RATE_LIMITED"):
                adapter.search()
        self.assertEqual(adapter.health(), "SOURCE_RATE_LIMITED")

    def test_empty_board_collection_preserves_provider_company_name(self):
        def empty_board(request):
            return httpx.Response(200, json={"jobs": []}) if request.url.path.endswith("/jobs") else httpx.Response(200, json={"name": "Employer With No Openings"})
        batch = Greenhouse("example", transport=httpx.MockTransport(empty_board)).collect()
        self.assertEqual(batch["company"], "Employer With No Openings")
        self.assertEqual(batch["jobs"], [])

    def test_collection_preserves_provenance_and_labels_live_matching(self):
        source = configure_source(self.store, "example")
        result = collect_source(self.store, source, "fixture", transport=httpx.MockTransport(self.transport))
        self.assertEqual(result["jobs_seen"], 2)
        self.assertEqual(self.store.count("canonical_jobs"), 2)
        self.assertEqual(self.store.count("job_postings"), 2)
        self.assertEqual(self.store.count("posting_snapshots"), 2)
        self.assertEqual(self.store.count("company_job_sources"), 1)
        job_id = identity(identity("greenhouse", "example"), "greenhouse", "example", "1234")
        self.assertEqual(self.store.job(job_id)["requisition"], "1234")
        with self.store.engine.connect() as connection:
            snapshots = list(connection.execute(select(m.posting_snapshots)).mappings())
            posting = dict(connection.execute(select(m.postings).where(m.postings.c.source_job_id == "1234")).mappings().one())
            match = connection.execute(select(m.matches)).mappings().first()
        self.assertEqual(snapshots[0]["raw"], RAW)
        self.assertEqual(posting["source_url"], "https://boards-api.greenhouse.io/v1/boards/example/jobs")
        self.assertEqual(posting["application_url"], RAW["absolute_url"])
        self.assertEqual(posting["raw"], RAW)
        self.assertEqual(len(snapshots[0]["sha256"]), 64)
        self.assertEqual(match["components"]["method"], "description_skill_mentions")
        self.assertTrue(all("jobs" in str(request.url) or str(request.url).endswith("/example") for request in self.requests))
        self.assertFalse(Greenhouse("example").capabilities().supports(SourceCapability.DIRECT_APPLICATION))

    def test_multiple_postings_share_one_canonical_job_and_route(self):
        source = configure_source(self.store, "example")
        collect_source(self.store, source, "fixture", transport=httpx.MockTransport(self.transport))
        job_id = identity(identity("greenhouse", "example"), "greenhouse", "example", "1234")
        raw = {"aggregator_id": "agg-1234", "title": "Python Engineer"}
        with self.store.engine.begin() as connection:
            persist_posting(connection, {
                "id": identity("aggregator", "agg-1234"), "job_id": job_id,
                "source": "https://aggregator.test/jobs", "source_job_id": "agg-1234",
                "source_url": "https://aggregator.test/jobs/agg-1234",
                "application_url": RAW["absolute_url"], "raw": raw, "fetched_at": now(),
            })
        self.assertEqual(len([row for row in self.store.jobs() if row["canonical_job_id"] == job_id]), 1)
        detail = self.store.job(job_id)
        self.assertEqual(len(detail["postings"]), 2)
        self.assertEqual({row["application_url"] for row in detail["postings"]}, {RAW["absolute_url"]})
        self.assertEqual({row["source_job_id"] for row in detail["postings"]}, {"1234", "agg-1234"})
        with self.store.engine.connect() as connection:
            self.assertEqual(len(list(connection.execute(select(m.routes).where(m.routes.c.job_id == job_id)))), 1)

    def test_worker_collection_uses_search_contract(self):
        source = configure_source(self.store, "example")
        with patch.object(Greenhouse, "collect", side_effect=AssertionError("legacy collect path called")):
            result = collect_source(self.store, source, "fixture", transport=httpx.MockTransport(self.transport))
        self.assertEqual(result["jobs_seen"], 2)
        self.assertEqual(self.store.count("canonical_jobs"), 2)

    def test_collector_persists_two_companies_from_one_adapter_batch(self):
        source_id = identity("synthetic", "mixed")
        with self.store.engine.begin() as connection:
            upsert(connection, m.job_sources, {"id": source_id, "provider": "synthetic", "tenant": "mixed",
                "name": "Mixed Feed", "enabled": True, "strategy": "PUBLIC_JSON",
                "capabilities": SourceCapabilities(CollectionStrategy.PUBLIC_JSON, frozenset({SourceCapability.DISCOVERY})).as_dict(),
                "poll_interval": 300, "concurrency": 1, "request_timeout": 10, "rate_limit": 60,
                "jitter_seconds": 0, "health_status": "NOT_CHECKED"})

        class MultiCompanyAdapter:
            def search(self):
                return SearchBatch("https://example.test/feed", (
                    SourceItem({"id": 1, "title": "Python Engineer"}, SourceCompany(("domain", "alpha.test"), "Alpha", "https://alpha.test")),
                    SourceItem({"id": 1, "title": "Platform Engineer"}, SourceCompany(("domain", "beta.test"), "Beta", "https://beta.test")),
                ))

            def normalize(self, raw):
                return {"requisition": str(raw["id"]), "title": raw["title"], "description": "Python systems",
                    "skills": ["Python"], "location": "Remote", "remote_type": "REMOTE", "application_url": "",
                    "attributes": {"provider": "synthetic", "fictional": False, "submission_capability": "MANUAL_ONLY"}}

            def capabilities(self):
                return SourceCapabilities(CollectionStrategy.PUBLIC_JSON, frozenset({SourceCapability.DISCOVERY}))

        module = importlib.import_module("services.discovery.sources.collect")
        self.assertTrue(hasattr(module, "source_adapter"))
        with patch.object(module, "source_adapter", return_value=MultiCompanyAdapter()):
            result = collect_source(self.store, source_id, "fixture")
        self.assertEqual((result["jobs_seen"], result["strategy"], result["direct_application"]), (2, "PUBLIC_JSON", False))
        self.assertEqual(self.store.count("companies"), 2)
        self.assertEqual(self.store.count("company_job_sources"), 2)
        self.assertEqual(self.store.count("canonical_jobs"), 2)
        self.assertEqual(self.store.count("job_postings"), 2)
        self.assertEqual(self.store.count("posting_snapshots"), 2)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.job_sources.c.name)).scalar_one(), "Mixed Feed")
            self.assertEqual({row["name"] for row in connection.execute(select(m.companies)).mappings()}, {"Alpha", "Beta"})

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

    def test_adapter_registry_rejects_unimplemented_provider(self):
        with self.assertRaisesRegex(ValueError, "SOURCE_UNSUPPORTED"):
            source_adapter({"provider": "unknown", "tenant": "example"})

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
        untrusted = adapter.normalize(dict(RAW, absolute_url="https://outside.test/apply"))
        self.assertEqual(untrusted["application_url"], "")
        self.assertEqual(untrusted["attributes"]["application_url_status"], "UNAVAILABLE")
        source = configure_source(self.store, "example")
        def response(request):
            return httpx.Response(200, json={"jobs": [RAW, dict(RAW, id=9, location=[])]}) if request.url.path.endswith("/jobs") else httpx.Response(200, json={"name": "Example"})
        # A nonempty malformed location is rejected while the valid row persists.
        def malformed(request):
            return httpx.Response(200, json={"jobs": [RAW, dict(RAW, id=9, location=["Remote"])]}) if request.url.path.endswith("/jobs") else response(request)
        result = collect_source(self.store, source, "fixture", transport=httpx.MockTransport(malformed))
        self.assertEqual((result["jobs_seen"], result["rejected"]), (1, 1))

    def test_cross_tenant_row_aborts_mixed_batch_before_any_company_or_job_write(self):
        source = configure_source(self.store, "example")
        self.bind_collector(self.store, source)
        before = {
            table: self.store.count(table)
            for table in (
                "companies", "company_job_sources", "canonical_jobs",
                "job_postings", "posting_snapshots",
            )
        }

        def mixed_routes(request):
            if request.url.path.endswith("/jobs"):
                foreign = dict(
                    RAW,
                    id=1235,
                    title="Foreign tenant role",
                    absolute_url="https://job-boards.greenhouse.io/other/jobs/1235",
                )
                return httpx.Response(200, json={"jobs": [RAW, foreign], "meta": {"total": 2}})
            return httpx.Response(200, json={"name": "Example Public Employer"})

        with self.assertRaises(SourceError) as raised:
            collect_source(
                self.store,
                source,
                "fixture",
                transport=httpx.MockTransport(mixed_routes),
            )
        self.assertEqual(raised.exception.code, "COMPANY_ATS_JOB_TENANT_MISMATCH")
        self.assertEqual(
            {
                table: self.store.count(table)
                for table in before
            },
            before,
        )

    def test_unbound_legacy_and_employer_routes_are_unavailable_and_never_persisted(self):
        source = configure_source(self.store, "legacy-example")
        rows = [
            dict(RAW, id=2001, internal_job_id=2001),
            dict(
                RAW,
                id=2002,
                internal_job_id=2002,
                absolute_url="https://employer.example/careers/python?gh_jid=2002",
            ),
        ]

        def response(request):
            if request.url.path.endswith("/jobs"):
                return httpx.Response(200, json={"jobs": rows, "meta": {"total": 2}})
            return httpx.Response(200, json={"name": "Legacy Employer"})

        result = collect_source(
            self.store, source, "fixture", transport=httpx.MockTransport(response),
        )
        self.assertEqual((result["jobs_seen"], result["rejected"]), (2, 0))
        with self.store.engine.connect() as connection:
            postings = list(connection.execute(select(m.postings)).mappings())
            routes = list(connection.execute(select(m.routes)).mappings())
            jobs = list(connection.execute(select(m.jobs)).mappings())
        self.assertEqual({row["application_url"] for row in postings}, {""})
        self.assertEqual({row["url"] for row in routes}, {""})
        self.assertEqual(
            {row["attributes"]["application_url_status"] for row in jobs}, {"UNAVAILABLE"},
        )

    def test_bound_and_unbound_concurrent_collectors_do_not_share_url_policy(self):
        other_directory = tempfile.TemporaryDirectory()
        other_store = Store(
            f"sqlite:///{(Path(other_directory.name) / 'unbound.db').as_posix()}"
        )
        other_store.migrate()
        other_store.save_profile(PROFILE)
        self.addCleanup(other_directory.cleanup)
        self.addCleanup(other_store.engine.dispose)

        bound_source = configure_source(self.store, "example")
        self.bind_collector(self.store, bound_source)
        unbound_source = configure_source(other_store, "legacy-example")
        foreign = dict(
            RAW,
            id=3001,
            internal_job_id=3001,
            absolute_url="https://job-boards.greenhouse.io/other/jobs/3001",
        )

        def transport(request):
            if request.url.path.endswith("/jobs"):
                return httpx.Response(200, json={"jobs": [foreign]})
            return httpx.Response(200, json={"name": "Concurrent Employer"})

        with ThreadPoolExecutor(max_workers=2) as executor:
            bound = executor.submit(
                collect_source,
                self.store,
                bound_source,
                "fixture",
                transport=httpx.MockTransport(transport),
            )
            unbound = executor.submit(
                collect_source,
                other_store,
                unbound_source,
                "fixture",
                transport=httpx.MockTransport(transport),
            )
            with self.assertRaises(SourceError) as raised:
                bound.result()
            unbound_result = unbound.result()

        self.assertEqual(raised.exception.code, "COMPANY_ATS_JOB_TENANT_MISMATCH")
        self.assertEqual(unbound_result["jobs_seen"], 1)
        with other_store.engine.connect() as connection:
            posting = connection.execute(select(m.postings)).mappings().one()
        self.assertEqual(posting["application_url"], "")

    def test_adapter_health_and_detail_validation(self):
        adapter = Greenhouse("example", transport=httpx.MockTransport(lambda request: httpx.Response(429) if request.url.path.endswith("/jobs") else httpx.Response(200, json={"name": "Example"})))
        with self.assertRaises(ValueError):
            adapter.collect()
        self.assertEqual(adapter.health(), "SOURCE_RATE_LIMITED")
        with self.assertRaisesRegex(ValueError, "SOURCE_INVALID_JOB"):
            Greenhouse("example", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"not": "job"}))).fetch(1234)


    def test_experience_salary_and_reported_date_normalize_with_provenance(self):
        raw = dict(
            RAW,
            title="Senior Platform Engineer",
            content="<p>Base compensation is $120,000-$150,000 per year.</p>",
            first_published_at="2026-09-30T14:00:00Z",
        )
        job = Greenhouse("example").normalize(raw)
        attrs = job["attributes"]
        self.assertEqual((attrs["experience_level"], attrs["experience_level_raw"]), ("SENIOR", "Senior"))
        self.assertEqual((attrs["salary_min"], attrs["salary_max"], attrs["salary_period"]), (120000.0, 150000.0, "YEAR"))
        self.assertEqual(attrs["salary_source"], "EMPLOYER_PROVIDED")
        self.assertEqual(attrs["reported_posted_at"], "2026-09-30T14:00:00+00:00")

    def test_collection_persists_first_and_last_seen_without_inventing_posted_date(self):
        source = configure_source(self.store, "example")
        collect_source(self.store, source, "fixture", transport=httpx.MockTransport(self.transport))
        with self.store.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(select(m.jobs)).mappings()]
        self.assertTrue(rows)
        for row in rows:
            attrs = row["attributes"]
            self.assertIsNotNone(attrs["first_seen_at"])
            self.assertIsNotNone(attrs["last_seen_at"])
            self.assertEqual(attrs["first_seen_at"], attrs["last_seen_at"])
            self.assertIsNone(attrs["reported_posted_at"])


    def test_skill_title_and_education_normalization_preserve_source_wording(self):
        raw = dict(
            RAW,
            title="Windows Sysadmin",
            content="<p>Manage Microsoft Active Directory, M365 and Rubrik. Bachelor's degree preferred.</p>",
        )
        job = Greenhouse("example").normalize(raw)
        attrs = job["attributes"]
        self.assertEqual(attrs["title_family"], "SYSTEMS_ADMINISTRATION")
        self.assertEqual(attrs["normalized_title"], "Systems Administrator")
        self.assertEqual(attrs["skill_ids"], ["ACTIVE_DIRECTORY", "MICROSOFT_365", "RUBRIK"])
        originals = {row["id"]: row["original"] for row in attrs["skill_mentions"]}
        self.assertEqual(originals["ACTIVE_DIRECTORY"], "Microsoft Active Directory")
        self.assertEqual(originals["MICROSOFT_365"], "M365")
        self.assertEqual(attrs["education_requirement"], "BACHELORS_PREFERRED")
        self.assertIn("Bachelor's degree preferred", attrs["education_requirement_raw"])

    def test_extreme_provider_backoff_cannot_be_shortened_by_reconfiguration(self):
        source = configure_source(self.store, "example")
        with self.assertRaisesRegex(ValueError, "SOURCE_RATE_LIMITED"):
            collect_source(self.store, source, "fixture", transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"retry-after": "100000000000000000000"})))
        configure_source(self.store, "example", enabled=True)
        with self.assertRaisesRegex(ValueError, "SOURCE_BACKOFF"):
            collect_source(self.store, source, "fixture", transport=httpx.MockTransport(lambda _: self.fail("No request during provider backoff")))

    def test_cancellation_during_rate_wait_prevents_the_next_request(self):
        network_requests = []
        check_count = [0]
        clock = [0.0]

        def check():
            check_count[0] += 1
            if check_count[0] >= 2:
                raise RuntimeError("CANCEL_REQUESTED")

        adapter = Greenhouse("example", check=check, request_timeout=2, rate_limit=1)
        adapter._last_request = 0.0

        def sleep(seconds):
            clock[0] += seconds

        with patch("services.discovery.sources.greenhouse.time.monotonic", side_effect=lambda: clock[0]), \
                patch("services.discovery.sources.greenhouse.time.sleep", side_effect=sleep), \
                patch("services.discovery.sources.greenhouse.httpx.Client",
                      side_effect=lambda *args, **kwargs: network_requests.append((args, kwargs))), \
                self.assertRaisesRegex(RuntimeError, "CANCEL_REQUESTED"):
            adapter._request("/jobs")

        self.assertEqual(network_requests, [])

    def _assert_local_stop_preserves_provider_health_and_failure_history(self, stop_type):
        source_id = configure_source(self.store, "example", rate_limit=1, jitter_seconds=0)
        checks = [0]

        def check():
            checks[0] += 1
            if checks[0] >= 3:
                raise stop_type("local operation stopped")

        adapter = Greenhouse("example", check=check, rate_limit=1)
        adapter._last_request = 0.0
        with patch("services.discovery.sources.collect.source_adapter", return_value=adapter), \
                patch("services.discovery.sources.greenhouse.time",
                      SimpleNamespace(monotonic=lambda: 0.0, sleep=Mock())) as clock, \
                self.assertRaises(stop_type):
            collect_source(self.store, source_id, "fixture", check=check)

        clock.sleep.assert_not_called()
        with self.store.engine.connect() as connection:
            source = dict(connection.execute(select(m.job_sources).where(m.job_sources.c.id == source_id)).mappings().one())
            observations = list(connection.execute(select(m.source_quality_observations).where(
                m.source_quality_observations.c.configured_source_id == source_id)))
        self.assertEqual(source["health_status"], "NOT_CHECKED")
        self.assertEqual(source["consecutive_failures"], 0)
        self.assertIsNone(source["last_error"])
        self.assertIsNone(source["backoff_until"])
        self.assertEqual(observations, [])

    def test_cancelled_collection_preserves_provider_health_and_failure_history(self):
        self._assert_local_stop_preserves_provider_health_and_failure_history(OperationCancelled)

    def test_timed_out_collection_preserves_provider_health_and_failure_history(self):
        self._assert_local_stop_preserves_provider_health_and_failure_history(OperationTimedOut)
