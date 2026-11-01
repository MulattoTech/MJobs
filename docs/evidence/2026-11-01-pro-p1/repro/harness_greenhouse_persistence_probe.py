"""Two bounded public Greenhouse GETs, then immutable offline collector replay.

Only tenant ``greenhouse`` and the two fixed public endpoints below are allowed.
Applicant profiles, application submission, AI providers and real databases are
not used. The unchanged reviewed helper supplies recording and persistence checks.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

HELPER_SHA256 = "4b9d77eed4483cc21fd6972f4c5086eab91ec8b18fd76d669f876d1a01bd003a"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/greenhouse"
JOBS_URL = BOARD_URL + "/jobs?content=true"
ENDPOINTS = (BOARD_URL, JOBS_URL)


def load_helper(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != HELPER_SHA256:
        raise ValueError("REVIEWED_HELPER_HASH_MISMATCH")
    spec = importlib.util.spec_from_file_location("mjobs_reviewed_public_probe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fixture_response(httpx, request):
    from tests.integration.test_greenhouse import RAW

    if str(request.url) == BOARD_URL:
        return httpx.Response(200, json={"name": "Synthetic Greenhouse Employer"})
    first = dict(RAW, absolute_url="https://job-boards.greenhouse.io/greenhouse/jobs/1234")
    second = dict(first, id=1235, title="Platform Engineer",
                  absolute_url="https://job-boards.greenhouse.io/greenhouse/jobs/1235")
    return httpx.Response(200, json={"jobs": [first, second], "meta": {"total": 2}})


def capture_transport(httpx, helper, check, fixture):
    from services.discovery.sources.http_transport import SourceTransport

    class TwoGetTransport(httpx.BaseTransport):
        def __init__(self):
            self.records = {}
            self.lock = threading.Lock()
            self.last_start = None

        def handle_request(self, request):
            check()
            url = str(request.url)
            helper.require(request.method == "GET" and url in ENDPOINTS, "UNEXPECTED_GREENHOUSE_REQUEST")
            # The production adapter skips its limiter with an injected transport.
            # Retain its configured 60/minute start spacing for these two GETs.
            if not fixture and self.last_start is not None:
                while (wait := self.last_start + 1 - time.monotonic()) > 0:
                    check()
                    time.sleep(min(wait, 0.05))
            check()
            with self.lock:
                helper.require(url not in self.records, "EXTRA_GREENHOUSE_GET")
                delegate = (httpx.MockTransport(lambda req: fixture_response(httpx, req))
                            if fixture else SourceTransport(check))
                record = helper.recorder(httpx, delegate, url, check)
                self.records[url] = record
                self.last_start = time.monotonic()
            return record.handle_request(request)

        def close(self):
            # Each production _get owns an HTTPX Client. Close used connections;
            # the second allowed endpoint receives a fresh owned transport.
            with self.lock:
                records = list(self.records.values())
            for record in records:
                record.close()

    return TwoGetTransport()


def replay_transport(httpx, helper, snapshots):
    calls = {url: [] for url in ENDPOINTS}
    transports = {url: helper.offline_transport(httpx, snapshots[url], calls[url]) for url in ENDPOINTS}

    class Replay(httpx.BaseTransport):
        def handle_request(self, request):
            helper.require(str(request.url) in transports, "UNEXPECTED_GREENHOUSE_REPLAY")
            return transports[str(request.url)].handle_request(request)

        def close(self):
            for transport in transports.values():
                transport.close()

    return Replay(), calls


def load_captures(helper, path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    snapshots = {}
    for filename in manifest["metadata_files"]:
        helper.require(Path(filename).name == filename, "REPLAY_METADATA_FILENAME_INVALID")
        metadata = json.loads((path.parent / filename).read_text(encoding="utf-8"))
        url = metadata["url"]
        helper.require(url in ENDPOINTS and url not in snapshots, "REPLAY_ENDPOINT_INVALID")
        filename = metadata["body_file"]
        helper.require(Path(filename).name == filename, "REPLAY_BODY_FILENAME_INVALID")
        with (path.parent / filename).open("rb") as stream:
            body = stream.read(helper.MAX_BYTES + 1)
        helper.require(len(body) <= helper.MAX_BYTES and len(body) == metadata["wire_bytes"], "REPLAY_BODY_SIZE_INVALID")
        helper.require(hashlib.sha256(body).hexdigest() == metadata["wire_sha256"], "REPLAY_BODY_HASH_INVALID")
        headers = tuple(tuple(row) for row in metadata["headers"])
        helper.require(all(len(row) == 2 and row[0] in {"content-type", "content-encoding", "content-length"}
                           and isinstance(row[1], str) for row in headers), "REPLAY_HEADERS_INVALID")
        helper.require(type(metadata["status"]) is int, "REPLAY_STATUS_INVALID")
        snapshots[url] = helper.Snapshot(url, metadata["status"], headers, body)
    helper.require(set(snapshots) == set(ENDPOINTS), "REPLAY_ENDPOINT_MISSING")
    return snapshots, manifest


def run(args, helper):
    import httpx
    from packages.persistence import models as m
    from packages.persistence.store import Store, identity
    from services.discovery.sources.collect import collect_source, configure_source
    from services.discovery.sources.greenhouse import plain_text
    from services.discovery.sources.registry import source_adapter
    from sqlalchemy import update

    mode = ("RETAINED_CAPTURE_REPLAY" if args.replay_manifest else
            "FIXTURE_SELF_CHECK" if args.fixture_self_check else "LIVE_CAPTURE")
    record = {"provider": "greenhouse", "tenant": "greenhouse", "result": "FAIL", "capture_mode": mode,
              "external_submission_requests": 0, "phase": "CAPTURE"}
    check = helper.Budget(20)
    capture = None
    snapshots = {}
    egress_attempts = []
    guards = ExitStack()

    def deny_egress(*unused_args, **unused_kwargs):
        egress_attempts.append(True)
        raise AssertionError("OFFLINE_EGRESS_FORBIDDEN")

    def block_egress(stack):
        for target in ("socket.socket", "socket.create_connection", "subprocess.Popen"):
            stack.enter_context(patch(target, side_effect=deny_egress))

    try:
        if mode != "LIVE_CAPTURE":
            block_egress(guards)
        if args.replay_manifest:
            snapshots, original = load_captures(helper, args.replay_manifest)
            record["original_capture"] = original
            transport, capture_calls = replay_transport(httpx, helper, snapshots)
        else:
            capture = capture_transport(httpx, helper, check, args.fixture_self_check)
            transport = capture
        adapter = source_adapter({"provider": "greenhouse", "tenant": "greenhouse", "request_timeout": 6,
                                  "concurrency": 1, "rate_limit": 60}, transport=transport, check=check)
        batch = adapter.search()
        if capture is not None:
            helper.require(set(capture.records) == set(ENDPOINTS), "CAPTURE_ENDPOINT_MISSING")
            snapshots = {url: capture.records[url].freeze() for url in ENDPOINTS}
            metadata_paths = [Path(helper.retain_snapshot(snapshot, "greenhouse", args.capture_dir, mode))
                              for snapshot in snapshots.values()]
            manifest = {"capture_mode": mode, "observed_at": datetime.now(UTC).isoformat(),
                        "metadata_files": [path.name for path in metadata_paths]}
            manifest_path = args.capture_dir / f"manifest-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
            with manifest_path.open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, indent=2)
            record["capture_manifest"] = str(manifest_path)
        else:
            helper.require(all(len(calls) == 1 for calls in capture_calls.values()), "CAPTURE_REPLAY_COUNT_INVALID")
        record["responses"] = [{"endpoint": url, "status": snapshot.status, "wire_bytes": len(snapshot.body),
                                "wire_sha256": hashlib.sha256(snapshot.body).hexdigest()} for url, snapshot in snapshots.items()]
        record["phase"] = "NORMALIZE_CAPTURE"
        helper.require(0 < len(batch.items) <= helper.MAX_JOBS and adapter.health() == "HEALTHY", "CAPTURE_UNHEALTHY")
        helper.require(batch.source_company and batch.source_company.identity_parts == ("greenhouse", "greenhouse"),
                       "BOARD_COMPANY_IDENTITY_INVALID")
        board = snapshots[BOARD_URL]
        response = httpx.Response(board.status, headers=board.headers, stream=httpx.ByteStream(board.body))
        try:
            response.read()
            company_name = response.json().get("name")
        finally:
            response.close()
        helper.require(isinstance(company_name, str) and company_name.strip(), "BOARD_COMPANY_NAME_MISSING")
        helper.require(batch.source_company.name == plain_text(company_name)[:200], "BOARD_COMPANY_NAME_CHANGED")
        source_id = identity("greenhouse", "greenhouse")
        company_id = identity(*batch.source_company.identity_parts)
        companies_hash = helper.digest([{"id": company_id, "name": batch.source_company.name,
                                         "domain": batch.source_company.domain}])
        expected_postings, expected_snapshots, normalized = {}, {}, []
        for item in batch.items:
            check()
            helper.require(item.company == batch.source_company, "JOB_COMPANY_ATTRIBUTION_CHANGED")
            job = adapter.normalize(item.raw)
            helper.require(job["title"] and job["requisition"] and isinstance(job["description"], str), "NORMALIZATION_INVALID")
            helper.require(job["attributes"]["submission_capability"] == "MANUAL_ONLY", "SUBMISSION_CAPABILITY_INVALID")
            # Company-specific Greenhouse uses native requisition directly; it
            # must not use the aggregator hash(company_id, requisition) identity.
            posting_id = identity(source_id, job["requisition"])
            expected_postings[posting_id] = {"id": posting_id, "source_job_id": job["requisition"]}
            raw_hash = helper.digest(item.raw)
            snapshot_id = identity(posting_id, raw_hash)
            expected_snapshots[snapshot_id] = {"id": snapshot_id, "posting_id": posting_id, "sha256": raw_hash}
            normalized.append(job)
        helper.require(any(job["description"] for job in normalized), "DESCRIPTIONS_MISSING")
        postings_hash = helper.digest([expected_postings[key] for key in sorted(expected_postings)])
        snapshots_hash = helper.digest([expected_snapshots[key] for key in sorted(expected_snapshots)])
        record["capture"] = {"jobs_returned": len(batch.items), "source_health": adapter.health(),
            "unique_source_identities": len(expected_postings), "unique_posting_raw_snapshots": len(expected_snapshots),
            "company_id_sha256": helper.digest(company_id), "company_name_sha256": helper.digest(batch.source_company.name),
            "normalization_sha256": helper.digest(normalized), "expected_source_identity_sha256": postings_hash,
            "expected_snapshots_sha256": snapshots_hash}
        with tempfile.TemporaryDirectory(prefix="mjobs-greenhouse-capture-") as directory:
            database_url = f"sqlite:///{(Path(directory) / 'isolated.db').as_posix()}"
            store = Store(database_url)
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                actual_source = configure_source(store, "greenhouse", provider="greenhouse", request_timeout=6,
                                                 poll_interval=300, concurrency=1, rate_limit=60, jitter_seconds=0)
                helper.require(actual_source == source_id, "SOURCE_ID_CHANGED")
                with ExitStack() as offline:
                    block_egress(offline)
                    baseline = None
                    for number in (1, 2):
                        record["phase"] = f"PERSIST_{number}"
                        if number == 2:
                            with store.engine.begin() as connection:
                                connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
                                    last_attempt=(datetime.now(UTC) - timedelta(seconds=301)).isoformat()))
                        transport, calls = replay_transport(httpx, helper, snapshots)
                        result = collect_source(store, source_id, None, refresh_matches=False,
                                                check=helper.Budget(120), transport=transport)
                        helper.require(result["jobs_seen"] == len(batch.items) and result["rejected"] == 0
                                       and not result["direct_application"], "COLLECTOR_COUNT_OR_CAPABILITY_CHANGED")
                        helper.require(all(len(rows) == 1 for rows in calls.values()), "REPLAY_REQUEST_COUNT_CHANGED")
                        state = helper.stable_database_state(store, source_id)
                        helper.require(state["source_copies"] == len(expected_postings)
                                       and state["source_identity_sha256"] == postings_hash, "SOURCE_IDENTITIES_CHANGED")
                        helper.require(state["posting_snapshots"] == len(expected_snapshots)
                                       and state["snapshots_sha256"] == snapshots_hash, "SNAPSHOT_IDENTITIES_CHANGED")
                        helper.require(state["companies"] == 1 and state["company_ids_sha256"] == helper.digest([company_id]),
                                       "COMPANY_ATTRIBUTION_CHANGED")
                        helper.require(state["companies_sha256"] == companies_hash, "COMPANY_NAME_OR_DOMAIN_CHANGED")
                        helper.require(state["source_jobs_seen"] == len(batch.items), "PERSISTED_SOURCE_COUNT_CHANGED")
                        helper.require(0 < state["canonical_jobs"] <= state["source_copies"], "CANONICAL_COUNT_INVALID")
                        store.engine.dispose()
                        store = Store(database_url)
                        helper.require(helper.stable_database_state(store, source_id) == state, "REOPEN_INVENTORY_CHANGED")
                        if baseline is not None:
                            helper.require(state == baseline, "SAME_CAPTURE_REPLAY_CHANGED_DURABLE_IDENTITIES")
                        baseline = state
                        record[f"persisted_pass_{number}"] = state
            finally:
                store.engine.dispose()
        helper.require(not egress_attempts, "OFFLINE_EGRESS_ATTEMPTED")
        record.update(result="PASS", phase="COMPLETE", collector_passes=2, database_reopens=2,
                      temporary_database_removed=True)
    except Exception as error:  # noqa: BLE001 -- sanitize every failure and return a nonzero command result
        record.update(error_type=type(error).__name__, error_code=str(getattr(error, "code", "PROBE_FAILED")))
        if isinstance(error, AssertionError) and str(error).isidentifier():
            record["error_code"] = str(error)
    finally:
        if capture is not None:
            capture.close()
        guards.close()
        record["external_get_attempts"] = len(capture.records) if mode == "LIVE_CAPTURE" and capture else 0
        record["offline_egress_attempts"] = len(egress_attempts)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--capture-dir", required=True, type=Path)
    parser.add_argument("--helper", type=Path, default=Path(__file__).with_name("harness_live_persistence_probe.py"))
    parser.add_argument("--fixture-self-check", action="store_true")
    parser.add_argument("--replay-manifest", type=Path)
    args = parser.parse_args()
    if args.evidence.exists():
        parser.error("--evidence must be new; previous reports are preserved")
    if args.replay_manifest and args.fixture_self_check:
        parser.error("Choose retained replay or fixture self-check")
    helper = load_helper(args.helper)
    sys.path.insert(0, str(args.repo_root.resolve()))
    report = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.repo_root, text=True).strip(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "helper_sha256": HELPER_SHA256,
              "observed_at": datetime.now(UTC).isoformat(),
              "mode": ("RETAINED_CAPTURE_OFFLINE_PERSISTED_REPLAY" if args.replay_manifest else
                       "FIXTURE_SELF_CHECK" if args.fixture_self_check else "LIVE_GREENHOUSE_CAPTURE_PERSISTED_REPLAY"),
              "budgets": {"live_gets": 2, "live_seconds": 20, "request_timeout_seconds": 6,
                          "concurrency": 1, "request_start_spacing_seconds": 1,
                          "wire_and_decoded_bytes_per_response": helper.MAX_BYTES, "max_jobs": helper.MAX_JOBS,
                          "offline_seconds_per_pass": 120}}
    started = time.monotonic()
    report["source"] = run(args, helper)
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    with args.evidence.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps(report), flush=True)
    return 0 if report["source"]["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
