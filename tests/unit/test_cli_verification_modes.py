"""Mode authority and combined reports must not invent current execution."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import socketserver
import sys
import threading
import time

import httpx
import pytest

from cli import verify_readiness as cli
from cli.verification_checks import CHECKS, DRY_RUN_CHECKS, new_report, validate_report


REVISION = "a" * 40


def functional_report():
    report = new_report("current", REVISION, "inputs")
    for row in report["checks"].values():
        row.update(status="PASS", reason="synthetic unit contract")
    report["cleanup"] = {"status": "PASS", "reason": "synthetic unit contract"}
    return report


def runtime_report():
    return {"ready": True, "checks": {"Application worker": "PASS"},
            "deployment": {"commit": REVISION, "version": "1.0.0", "operation_mode": "PERSONAL"}}


def ready_payload():
    return {"status": "ready", "db_backend": "postgresql", "checks": {
        "configuration": True, "schema": True, "database": True, "queue": True,
        "scheduler": True, "worker": True, "workers": {name: True for name, _ in cli._QUEUE_LABELS}},
        "private": "do-not-return-this"}


def json_stream(value):
    return httpx.Response(200, stream=httpx.ByteStream(json.dumps(value).encode()))


def test_dry_run_has_a_distinct_exact_inventory_and_cannot_be_changed_by_child():
    expected = new_report("invocation", REVISION, "inputs", mode="DRY_RUN")
    assert set(expected["checks"]) == set(DRY_RUN_CHECKS)
    assert "mock confirmation" not in expected["checks"]
    assert "duplicate-submission protection" not in expected["checks"]
    assert "Scoring functional" in expected["checks"]
    received = deepcopy(expected)
    received.update(runtime={"pid": 123, "python": sys.version.split()[0]},
                    finished_at=datetime.now(timezone.utc).isoformat())
    validate_report(received, expected, pid=123)
    received["execution_mode"] = "CONNECTED"
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_report(received, expected, pid=123)
    received["execution_mode"] = "DRY_RUN"
    received["checks"]["mock confirmation"] = {"status": "PASS", "reason": "forged"}
    with pytest.raises(ValueError, match="check set"):
        validate_report(received, expected, pid=123)
    with pytest.raises(ValueError):
        new_report("invocation", REVISION, "inputs", mode="unknown")


@pytest.mark.parametrize("failure", [None, "runtime", "missing", "cleanup", "different",
                                         "changed", "dirty", "missing_identity"])
def test_full_report_requires_live_health_executed_checks_cleanup_and_same_source(monkeypatch, failure):
    before, after = runtime_report(), runtime_report()
    functional = functional_report()
    if failure == "runtime":
        after["ready"] = False
        after["checks"]["Application worker"] = "FAIL"
    if failure == "missing":
        functional["checks"]["Browser worker healthy"]["status"] = "NOT_RUN"
    if failure == "cleanup":
        functional["cleanup"]["status"] = "BLOCKED"
    if failure == "different":
        functional["revision"] = "b" * 40
    if failure == "changed":
        after["deployment"]["commit"] = "b" * 40
    if failure == "missing_identity":
        before["deployment"] = after["deployment"] = None
    observations = iter([before, after])
    monkeypatch.setattr(cli, "collect_readiness", lambda *_args, **_kwargs: next(observations))
    monkeypatch.setattr(cli, "working_tree_clean", lambda: failure != "dirty")
    monkeypatch.setattr("cli.verification_checks.source_identity", lambda: (REVISION, "inputs"))
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return functional

    monkeypatch.setattr("cli.verification_checks.run_connected", execute)
    result = cli.run_full("http://127.0.0.1:18801", fixture=True)
    assert calls == [{"fixture": True, "timeout": 120}]
    assert result["runtime_after"] is after
    assert result["functional"] is functional
    assert set(functional["checks"]) == set(CHECKS)
    assert result["status"] == ("PASS" if failure is None else "FAIL")
    assert (result["exit_code"] == 0) is (failure is None)
    if failure == "missing":
        assert result["exit_code"] == 2
    if failure == "runtime":
        assert result["functional"]["checks"]["Browser worker healthy"]["status"] == "PASS"
        assert result["runtime_after"]["checks"]["Application worker"] == "FAIL"


@pytest.mark.parametrize("change", ["transient_edit", "checkout_after_functional"])
def test_full_rejects_source_changes_during_live_probes_even_when_final_tree_is_clean(monkeypatch, change):
    source = {"revision": REVISION, "digest": "inputs", "clean": True}
    probes = []
    functional = functional_report()

    def observe(*_args, **_kwargs):
        probes.append(len(probes) + 1)
        if change == "transient_edit":
            # The child sees one stable edited tree throughout its execution;
            # both outer cleanliness observations still see the original tree.
            source.update(digest="edited-inputs" if len(probes) == 1 else "inputs",
                          clean=len(probes) == 2)
        elif len(probes) == 2:
            # A clean checkout can change after the child has returned without
            # changing the live deployment or the child's reported revision.
            source.update(revision="b" * 40, digest="other-revision-inputs")
        return runtime_report()

    def execute(**_kwargs):
        functional.update(revision=source["revision"], source_sha256=source["digest"])
        return functional

    monkeypatch.setattr(cli, "collect_readiness", observe)
    monkeypatch.setattr(cli, "working_tree_clean", lambda: source["clean"])
    monkeypatch.setattr("cli.verification_checks.source_identity",
                        lambda: (source["revision"], source["digest"]))
    monkeypatch.setattr("cli.verification_checks.run_connected", execute)
    result = cli.run_full("http://127.0.0.1:18801", fixture=True)
    assert probes == [1, 2]
    assert result["runtime_before"]["ready"] and result["runtime_after"]["ready"]
    assert all(row["status"] == "PASS" for row in functional["checks"].values())
    assert functional["cleanup"]["status"] == "PASS"
    assert result["revision_identity"]["working_tree_clean"]
    assert result["status"] == "FAIL" and result["exit_code"] == 1
    assert result["revision_identity"]["status"] == "FAIL"
    assert result["revision_identity"]["source_before"] == {"revision": REVISION, "source_sha256": "inputs"}
    assert result["revision_identity"]["source_after"] == {
        "revision": source["revision"], "source_sha256": source["digest"]}
    assert result["revision_identity"]["functional_inputs_match"] is False


def test_live_collection_only_retains_valid_identity_and_structural_checks(monkeypatch):
    client = httpx.Client
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/readiness":
            return json_stream(ready_payload())
        return json_stream({**runtime_report()["deployment"], "private": "do-not-return-this"})

    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(respond)))
    result = cli.collect_readiness("http://127.0.0.1:18801", include_identity=True)
    assert result["ready"]
    assert requests == ["/readiness", "/__deployment"]
    assert result["deployment"] == runtime_report()["deployment"]
    assert "do-not-return-this" not in repr(result)


@pytest.mark.parametrize("identity", [[], {}, "transport_failure"])
def test_unavailable_or_malformed_identity_preserves_observed_readiness(monkeypatch, identity):
    client = httpx.Client

    def respond(request):
        if request.url.path == "/readiness":
            return json_stream(ready_payload())
        if identity == "transport_failure":
            raise httpx.ConnectError("do-not-return-this")
        return json_stream({**runtime_report()["deployment"], "operation_mode": identity})

    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(respond)))
    result = cli.collect_readiness("http://127.0.0.1:18801", include_identity=True)
    assert result["checks"]["PostgreSQL reachable"] == "PASS"
    assert result["checks"]["Deployment identity"] == "NOT_RUN"
    assert not result["ready"] and result["deployment"] is None
    assert "do-not-return-this" not in repr(result)


@pytest.mark.parametrize("endpoint", ["/readiness", "/__deployment"])
def test_deeply_nested_json_is_an_unavailable_probe_without_losing_other_evidence(monkeypatch, endpoint):
    client = httpx.Client
    nested = b"[" * 10000 + b"0" + b"]" * 10000
    assert len(nested) < cli.MAX_RESPONSE_BYTES
    with pytest.raises(RecursionError):
        json.loads(nested)

    def respond(request):
        if request.url.path == endpoint:
            return httpx.Response(200, stream=httpx.ByteStream(nested))
        return json_stream(ready_payload() if request.url.path == "/readiness"
                           else runtime_report()["deployment"])

    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(respond)))
    result = cli.collect_readiness("http://127.0.0.1:18801", include_identity=True)
    assert not result["ready"]
    assert result["checks"]["PostgreSQL reachable"] == ("NOT_RUN" if endpoint == "/readiness" else "PASS")
    assert result["checks"]["Deployment identity"] == ("NOT_RUN" if endpoint == "/__deployment" else "PASS")


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "999999"}, {"Content-Encoding": "gzip"}])
def test_runtime_body_is_bounded_and_compressed_bodies_are_not_decompressed(monkeypatch, headers):
    client = httpx.Client

    class Body(httpx.SyncByteStream):
        chunks = 0
        closed = False

        def __iter__(self):
            for _ in range(100):
                self.chunks += 1
                yield b" " * 4096

        def close(self):
            self.closed = True

    body = Body()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=body))))
    result = cli.collect_readiness("http://127.0.0.1:18801")
    assert not result["ready"]
    assert body.closed and body.chunks <= 17
    if headers:
        assert body.chunks == 0


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_real_slow_drip_is_stopped_by_absolute_runtime_deadline(phase):
    release = threading.Event()
    connected = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            self.request.recv(4096)
            connected.set()
            prefix = (b"HTTP/1.1 200 OK\r\nX-Slow: " if phase == "headers" else
                      b"HTTP/1.1 200 OK\r\nContent-Length: 9999\r\n\r\n{")
            try:
                self.request.sendall(prefix)
                while not release.wait(.02):
                    self.request.sendall(b" ")
            except OSError:
                pass

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        try:
            started = time.monotonic()
            result = cli.collect_readiness(f"http://127.0.0.1:{server.server_address[1]}", timeout=.25)
            elapsed = time.monotonic() - started
            assert connected.is_set(), "fixture did not reach the measured transport phase"
            assert elapsed < 1.0, (phase, elapsed)
            assert not result["ready"]
            assert not any(t.name == "mjobs-request-deadline" for t in threading.enumerate())
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize("args", [["--full"], ["--dry-run"], ["--full", "--dry-run", "--fixture"],
    ["--dry-run", "--fixture", "--api", "https://employer.invalid"],
    ["--full", "--fixture", "--api", "https://secret@example.invalid"],
    ["--full", "--fixture", "--api", "http://127.0.0.1:invalid"],
    ["--full", "--fixture", "--api", "http://127.0.0.1:99999"]])
def test_invalid_mode_admission_cannot_touch_live_or_fixture_services(monkeypatch, args):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid admission reached execution")

    monkeypatch.setattr(cli, "collect_readiness", forbidden)
    monkeypatch.setattr("cli.verification_checks.run_connected", forbidden)
    with pytest.raises(SystemExit) as result:
        cli.main(args)
    assert result.value.code == 2
