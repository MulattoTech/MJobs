"""Bounded public capture followed by real, network-denied collector replay.

Only the LIVE_CAPTURE phase contacts three fixed public listing endpoints. Each
response is retained as immutable in-memory bytes and replayed, unmodified,
through the production adapters and collect_source into temporary SQLite.
No applicant profile, application, provider/AI call, or real database is used.
The optional careers check has its own robots-aware, same-origin read budget.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ENDPOINTS = {
    "remotive": "https://remotive.com/api/remote-jobs",
    "wwr": "https://weworkremotely.com/remote-jobs.rss",
    "remoteok": "https://remoteok.com/api",
}
CAREERS_URL = "https://www.greenhouse.com/careers/opportunities"
MAX_BYTES = 5_000_000
MAX_JOBS = 1000


def require(condition, code):
    if not condition:
        raise AssertionError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


class Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def __call__(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("PROBE_DEADLINE")

    def remaining_seconds(self):
        self()
        return self.deadline - time.monotonic()


@dataclass(frozen=True)
class Snapshot:
    url: str
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


def recorder(httpx, delegate, expected_url, check):
    class RecordingTransport(httpx.BaseTransport):
        def __init__(self):
            self.calls = 0
            self.body = bytearray()
            self.response = None
            self.complete = False

        def handle_request(self, request):
            check()
            require(self.calls == 0, "EXTRA_LIVE_GET_FORBIDDEN")
            require(request.method == "GET" and str(request.url) == expected_url,
                    "UNEXPECTED_LIVE_REQUEST")
            require(not any(key in request.headers for key in ("authorization", "cookie")),
                    "CREDENTIAL_HEADER_FORBIDDEN")
            self.calls += 1
            response = delegate.handle_request(request)
            self.response = response
            owner = self

            class CaptureStream(httpx.SyncByteStream):
                def __iter__(self):
                    for chunk in response.stream:
                        check()
                        require(len(owner.body) + len(chunk) <= MAX_BYTES,
                                "WIRE_BYTE_BUDGET")
                        owner.body.extend(chunk)
                        yield chunk
                    owner.complete = True

                def close(self):
                    response.close()

            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=CaptureStream(), request=request,
                                  extensions=response.extensions)

        def close(self):
            delegate.close()

        def freeze(self):
            require(self.calls == 1 and self.complete, "INCOMPLETE_CAPTURE")
            # Cookies and unrelated server headers are never retained or reported.
            headers = tuple((key, value) for key, value in self.response.headers.multi_items()
                            if key in {"content-type", "content-encoding", "content-length"})
            return Snapshot(expected_url, self.response.status_code, headers, bytes(self.body))

    return RecordingTransport()


def offline_transport(httpx, snapshot, calls):
    def reply(request):
        require(request.method == "GET" and str(request.url) == snapshot.url,
                "REPLAY_REQUEST_DIFFERS_FROM_CAPTURE")
        calls.append((request.method, str(request.url)))
        require(len(calls) == 1, "EXTRA_REPLAY_REQUEST")
        # A stream preserves encoded wire bytes; HTTPX performs the same decoding.
        return httpx.Response(snapshot.status, headers=snapshot.headers,
                              stream=httpx.ByteStream(snapshot.body), request=request)
    return httpx.MockTransport(reply)


def stable_database_state(store, source_id):
    from packages.persistence import models as m
    from sqlalchemy import func, select

    with store.engine.connect() as connection:
        jobs = [dict(row) for row in connection.execute(select(
            m.jobs.c.id, m.jobs.c.company_id, m.jobs.c.requisition, m.jobs.c.title,
            m.jobs.c.description, m.jobs.c.location, m.jobs.c.remote_type,
            m.jobs.c.skills).order_by(m.jobs.c.id)).mappings()]
        copies = [dict(row) for row in connection.execute(select(
            m.postings.c.id, m.postings.c.job_id, m.postings.c.source,
            m.postings.c.source_job_id, m.postings.c.application_url, m.postings.c.raw
        ).order_by(m.postings.c.id)).mappings()]
        for row in copies:
            row["raw_sha256"] = digest(row.pop("raw"))
        snapshots = [dict(row) for row in connection.execute(select(
            m.posting_snapshots.c.id, m.posting_snapshots.c.posting_id,
            m.posting_snapshots.c.sha256).order_by(m.posting_snapshots.c.id)).mappings()]
        companies = [dict(row) for row in connection.execute(select(
            m.companies.c.id, m.companies.c.name, m.companies.c.domain
        ).order_by(m.companies.c.id)).mappings()]
        source = dict(connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == source_id)).mappings().one())
        for table in (m.applications, m.attempts):
            require(connection.execute(select(func.count()).select_from(table)).scalar_one() == 0,
                    "APPLICATION_RECORD_CREATED")
    require(jobs and copies and companies, "EMPTY_PERSISTED_INVENTORY")
    require(all(row["title"] and row["requisition"] for row in jobs), "PERSISTED_NORMALIZATION")
    require(all(row["job_id"] in {job["id"] for job in jobs} for row in copies), "ORPHAN_SOURCE_COPY")
    require(all(row["company_id"] in {company["id"] for company in companies} for row in jobs),
            "ORPHAN_COMPANY")
    require(source["health_status"] == "HEALTHY" and source["consecutive_failures"] == 0,
            "PERSISTED_SOURCE_UNHEALTHY")
    require(source["last_success"] and source["last_error"] is None, "SOURCE_SUCCESS_MISSING")
    return {
        "canonical_jobs": len(jobs), "source_copies": len(copies),
        "posting_snapshots": len(snapshots), "companies": len(companies),
        "canonical_sha256": digest(jobs), "source_copies_sha256": digest(copies),
        "snapshots_sha256": digest(snapshots), "companies_sha256": digest(companies),
        "source_health": source["health_status"], "source_jobs_seen": source["jobs_seen"],
        "application_records": 0, "application_attempts": 0,
    }


def fixture_delegate(httpx, provider):
    # Explicit self-check fixtures are never represented as live-source evidence.
    from tests.integration.test_remoteok import RAW_REMOTEOK_PAYLOAD
    from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
    from tests.integration.test_wwr import RAW_WWR_RSS

    def reply(request):
        if provider == "wwr":
            return httpx.Response(200, content=RAW_WWR_RSS,
                                  headers={"content-type": "application/rss+xml"})
        return httpx.Response(200, json=(RAW_REMOTIVE_PAYLOAD if provider == "remotive"
                                        else RAW_REMOTEOK_PAYLOAD))
    return httpx.MockTransport(reply)


def verify_source(provider, *, fixture=False):
    import httpx
    from packages.persistence import models as m
    from packages.persistence.store import Store, identity
    from services.discovery.sources.collect import collect_source, configure_source
    from services.discovery.sources.http_transport import SourceTransport
    from services.discovery.sources.registry import source_adapter
    from sqlalchemy import update

    start = time.monotonic()
    record = {"provider": provider, "result": "FAIL", "endpoint": ENDPOINTS[provider],
              "capture_mode": "FIXTURE_SELF_CHECK" if fixture else "LIVE_CAPTURE",
              "replay_mode": "IMMUTABLE_CAPTURE_WITH_NETWORK_DENIED"}
    check = Budget(20)
    capture = recorder(httpx, fixture_delegate(httpx, provider) if fixture else SourceTransport(check),
                       ENDPOINTS[provider], check)
    try:
        adapter = source_adapter({"provider": provider, "tenant": "default", "request_timeout": 6,
                                  "rate_limit": 60, "concurrency": 1}, transport=capture, check=check)
        # One exact unfiltered native endpoint; collector.search() uses the same URL.
        batch = adapter.search()
        require(0 < len(batch.items) <= MAX_JOBS, "LIVE_JOB_COUNT_BUDGET")
        snapshot = capture.freeze()
        require(adapter.health() == "HEALTHY", "LIVE_SOURCE_UNHEALTHY")
        company_ids = set()
        expected_postings = set()
        descriptions = known_companies = 0
        normalized = []
        for item in batch.items:
            check()
            job = adapter.normalize(item.raw)
            require(job["title"] and job["requisition"] and isinstance(job["description"], str),
                    "LIVE_NORMALIZATION_INVALID")
            require(item.company.name and item.company.identity_parts, "LIVE_COMPANY_INVALID")
            require(job["attributes"]["submission_capability"] == "MANUAL_ONLY", "DIRECT_SUBMISSION_CAPABILITY")
            company_id = identity(*item.company.identity_parts)
            company_ids.add(company_id)
            expected_postings.add((company_id, job["requisition"]))
            descriptions += bool(job["description"])
            known_companies += item.company.name.casefold() != "unknown company"
            normalized.append(job)
        require(descriptions > 0 and known_companies > 0, "NO_DESCRIPTIVE_COMPANY_JOB")
        require(len(expected_postings) == len(batch.items), "DUPLICATE_NATIVE_SOURCE_IDENTITIES")
        record["capture"] = {
            "get_requests": capture.calls, "external_get_requests": 0 if fixture else capture.calls,
            "response_status": snapshot.status, "wire_bytes": len(snapshot.body),
            "wire_sha256": hashlib.sha256(snapshot.body).hexdigest(),
            "jobs_returned": len(batch.items), "company_identities": len(company_ids),
            "known_company_jobs": known_companies, "jobs_with_description": descriptions,
            "normalization_sha256": digest(normalized), "source_health": adapter.health(),
        }
        egress_attempts = []

        def deny_egress(*args, **kwargs):
            egress_attempts.append(True)
            raise AssertionError("OFFLINE_EGRESS_FORBIDDEN")

        with tempfile.TemporaryDirectory(prefix="mjobs-public-capture-") as directory:
            database_url = f"sqlite:///{(Path(directory) / 'isolated.db').as_posix()}"
            store = Store(database_url)
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                source_id = configure_source(store, "default", provider=provider, request_timeout=6,
                                             poll_interval=300, concurrency=1, rate_limit=60, jitter_seconds=0)
                with ExitStack() as denied:
                    for target in ("socket.socket", "socket.create_connection", "subprocess.Popen"):
                        denied.enter_context(patch(target, side_effect=deny_egress))
                    baseline = None
                    for number in (1, 2):
                        if number == 2:
                            # Advance only this throwaway source's last-attempt timestamp.
                            # This models an eligible later poll with ZERO provider calls.
                            with store.engine.begin() as connection:
                                connection.execute(update(m.job_sources).where(
                                    m.job_sources.c.id == source_id).values(last_attempt=(
                                        datetime.now(UTC) - timedelta(seconds=301)).isoformat()))
                        replay_calls = []
                        result = collect_source(store, source_id, None, refresh_matches=False,
                            check=Budget(120), transport=offline_transport(httpx, snapshot, replay_calls))
                        require(result["jobs_seen"] == len(batch.items) and result["rejected"] == 0,
                                "COLLECTOR_DROPPED_CAPTURED_JOBS")
                        require(not result["direct_application"] and len(replay_calls) == 1,
                                "COLLECTOR_READ_ONLY_CONTRACT")
                        state = stable_database_state(store, source_id)
                        require(state["source_copies"] == len(expected_postings), "SOURCE_COPY_COUNT_CHANGED")
                        require(state["companies"] == len(company_ids), "COMPANY_COUNT_CHANGED")
                        require(state["posting_snapshots"] == state["source_copies"], "SNAPSHOT_COUNT_CHANGED")
                        require(0 < state["canonical_jobs"] <= state["source_copies"], "CANONICAL_COUNT_INVALID")
                        store.engine.dispose()
                        store = Store(database_url)
                        require(stable_database_state(store, source_id) == state, "REOPEN_INVENTORY_CHANGED")
                        if baseline is not None:
                            require(state == baseline, "SAME_CAPTURE_REPLAY_CHANGED_DURABLE_IDENTITIES")
                        baseline = state
                        record[f"persisted_pass_{number}"] = state
                    require(not egress_attempts, "REPLAY_ATTEMPTED_EGRESS")
                record.update(result="PASS", persisted_replay="PASS", reopen_checks=2,
                              offline_egress_attempts=len(egress_attempts), external_submission_requests=0)
            finally:
                store.engine.dispose()
        record["temporary_database_removed"] = True
    except Exception as error:  # noqa: BLE001 -- emit a sanitized FAIL and keep other sources independent
        record.update(error_type=type(error).__name__, error_code=str(getattr(error, "code", "PROBE_FAILED")))
        if isinstance(error, AssertionError) and str(error).isidentifier():
            record["error_code"] = str(error)
    finally:
        capture.close()
        record["live_get_attempts"] = 0 if fixture else capture.calls
    record["duration_seconds"] = round(time.monotonic() - start, 3)
    return record


def verify_careers(prior_path):
    from services.ats_detection.board_links import greenhouse_board_tenants
    from services.ats_detection.detector import detect_ats
    from services.company_discovery.fetch_policy import bounded_get
    from services.company_discovery.public_client import (
        PublicCompanyClient,
        PublicFetchError,
    )

    record = {"result": "FAIL", "career_url": CAREERS_URL, "mode": "LIVE_PUBLIC_CAREERS"}
    client = None
    try:
        prior_bytes = prior_path.read_bytes()
        prior = json.loads(prior_bytes)
        require(prior.get("mode") == "PUBLIC_READ_ONLY" and prior.get("external_submission_requests") == 0,
                "PRIOR_LIVE_EVIDENCE_INVALID")
        inventory = next(row for row in prior["sources"] if row["provider"] == "greenhouse")
        require(inventory.get("result") == "PASS" and inventory.get("tenant") == "greenhouse"
                and inventory.get("endpoint") == "https://boards-api.greenhouse.io/v1/boards/greenhouse/jobs"
                and inventory.get("jobs_returned", 0) > 0, "PRIOR_GREENHOUSE_INVENTORY_INVALID")

        class TwoRequestClient(PublicCompanyClient):
            def _budget(self):
                if self._requests >= 2:
                    raise PublicFetchError("REQUEST_BUDGET")
                super()._budget()

        client = TwoRequestClient(CAREERS_URL, check=Budget(20), total_timeout=20)
        response = bounded_get(client, CAREERS_URL)
        require(response.status_code == 200 and "text/html" in response.headers.get("content-type", ""),
                "CAREERS_HTML_NOT_AVAILABLE")
        detection = detect_ats(str(response.url), response.text,
                              redirect_chain=[str(row.url) for row in response.history])
        tenants = greenhouse_board_tenants(str(response.url), response.text)
        require(detection.status == "MATCH" and detection.provider == "greenhouse", "CAREERS_ATS_NOT_MATCHED")
        require("greenhouse" in tenants, "CAREERS_TENANT_NOT_VERIFIED")
        record.update(result="PASS", response_status=response.status_code,
                      page_sha256=hashlib.sha256(response.content).hexdigest(),
                      ats_provider=detection.provider, ats_tenant="greenhouse", get_requests=client._requests,
                      jobs_enumerated=inventory["jobs_returned"],
                      enumeration_mode="SEPARATE_PRIOR_LIVE_API_CAPTURE_SAME_TENANT",
                      prior_evidence_sha256=hashlib.sha256(prior_bytes).hexdigest(),
                      prior_revision=prior["revision"], prior_observed_at=prior["observed_at"],
                      external_submission_requests=0)
    except Exception as error:  # noqa: BLE001 -- every failure is explicit and makes the command nonzero
        record.update(error_type=type(error).__name__, error_code=str(getattr(error, "code", "PROBE_FAILED")))
        if isinstance(error, AssertionError) and str(error).isidentifier():
            record["error_code"] = str(error)
    finally:
        if client is not None:
            record["get_requests"] = client._requests
            client.close()
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--fixture-self-check", action="store_true")
    parser.add_argument("--prior-live-evidence", type=Path,
                        help="Also check the independently verified official employer careers page.")
    parser.add_argument("--careers-only", action="store_true")
    args = parser.parse_args()
    if args.careers_only and args.prior_live_evidence is None:
        parser.error("--careers-only requires --prior-live-evidence")
    if args.fixture_self_check and args.prior_live_evidence:
        parser.error("Fixture self-check cannot request live employer reads")
    sys.path.insert(0, str(args.repo_root.resolve()))
    report = {
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.repo_root, text=True).strip(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "observed_at": datetime.now(UTC).isoformat(),
        "mode": "FIXTURE_SELF_CHECK" if args.fixture_self_check else "LIVE_CAPTURE_OFFLINE_PERSISTED_REPLAY",
        "budgets": {"live_gets_per_source": 1, "source_wall_seconds": 20,
                    "source_request_timeout_seconds": 6, "wire_and_decoded_bytes_per_source": MAX_BYTES,
                    "jobs_per_source": MAX_JOBS, "offline_seconds_per_pass": 120,
                    "careers_gets_including_robots": 2, "careers_wall_seconds": 20},
        "external_submission_requests": 0, "sources": [], "company": {"result": "NOT_RUN"},
        "dedupe_scope": "Exact captured posting replay retains canonical and source-copy identities; no cross-source coincidence claimed.",
    }
    if not args.careers_only:
        for provider in ENDPOINTS:
            record = verify_source(provider, fixture=args.fixture_self_check)
            report["sources"].append(record)
            print(json.dumps(record), flush=True)
    if args.prior_live_evidence:
        report["company"] = verify_careers(args.prior_live_evidence)
        print(json.dumps(report["company"]), flush=True)
    args.evidence.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    failed = any(row["result"] != "PASS" for row in report["sources"])
    failed |= args.prior_live_evidence is not None and report["company"]["result"] != "PASS"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
