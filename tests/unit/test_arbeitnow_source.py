"""Deterministic Arbeitnow contracts; synthetic fixture, no live network."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from unittest.mock import patch

import httpx
import pytest

from services.discovery.sources.arbeitnow import (
    API_URL, LISTING_HOSTS, MAX_PAGE_ITEMS, MAX_RESPONSE_BYTES,
    Arbeitnow, company_identity,
)
from services.discovery.sources.contract import CollectionStrategy, SourceCapability
from services.discovery.sources.errors import SourceError
from workers.operations import OperationCancelled, OperationTimedOut


@pytest.fixture
def payload():
    return json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "arbeitnow.json").read_text())


def adapter_for(payload, requests=None, **kwargs):
    requests = [] if requests is None else requests

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    return Arbeitnow(transport=httpx.MockTransport(respond), **kwargs)


def test_search_is_one_fixed_read_with_unchanged_raw_and_native_company_identity(payload):
    requests = []
    original = deepcopy(payload)
    adapter = adapter_for(payload, requests)
    assert adapter.health() == "NOT_CHECKED"
    batch = adapter.search()
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == API_URL
    assert requests[0].headers["accept"] == "application/json"
    assert "authorization" not in requests[0].headers
    assert batch.endpoint == API_URL
    assert batch.source_company is None
    assert batch.complete_inventory is False
    assert len(batch.items) == 2
    assert [item.raw for item in batch.items] == original["data"]
    assert batch.items[0].company.identity_parts == (
        "arbeitnow", "www.arbeitnow.com", "synthetic-acme-cloud")
    assert batch.items[1].company.identity_parts == (
        "arbeitnow", "www.arbeitnow.co.uk", "synthetic-northbridge")
    assert payload == original
    assert adapter.health() == "HEALTHY"


def test_source_declares_only_implemented_capabilities_and_no_detail_fetch(payload):
    requests = []
    adapter = adapter_for(payload, requests)
    assert adapter.capabilities().strategy == CollectionStrategy.PUBLIC_JSON
    assert adapter.capabilities().features == frozenset({
        SourceCapability.DISCOVERY, SourceCapability.REMOTE_METADATA,
        SourceCapability.APPLICATION_REDIRECT,
    })
    for job_id in (1, "senior-python-engineer-berlin-1001", None):
        with pytest.raises(SourceError, match="^SOURCE_UNSUPPORTED$"):
            adapter.fetch(job_id)
    assert requests == []
    assert adapter.health() == "NOT_CHECKED"


@pytest.mark.parametrize(("name", "value", "code"), [
    ("tenant", "", "TENANT"), ("tenant", "europe", "TENANT"),
    ("tenant", None, "TENANT"), ("tenant", True, "TENANT"),
    ("request_timeout", True, "REQUEST_TIMEOUT"), ("request_timeout", 1, "REQUEST_TIMEOUT"),
    ("request_timeout", 61, "REQUEST_TIMEOUT"), ("request_timeout", "10", "REQUEST_TIMEOUT"),
    ("rate_limit", False, "RATE_LIMIT"), ("rate_limit", 0, "RATE_LIMIT"),
    ("rate_limit", 601, "RATE_LIMIT"), ("rate_limit", "60", "RATE_LIMIT"),
    ("concurrency", True, "CONCURRENCY"), ("concurrency", 0, "CONCURRENCY"),
    ("concurrency", 9, "CONCURRENCY"),
])
def test_invalid_configuration_fails_before_egress(name, value, code):
    with pytest.raises(ValueError, match=f"^SOURCE_INVALID_{code}$"):
        Arbeitnow(**{name: value})


def test_normalization_preserves_source_facts_without_guessing_salary_or_country(payload):
    raw = payload["data"][0]
    original = deepcopy(raw)
    job = Arbeitnow().normalize(raw)
    assert raw == original
    assert job["requisition"] == raw["slug"]
    assert job["application_url"] == raw["url"]
    assert job["title"] == "Senior Python Engineer"
    assert "Python" in job["skills"]
    assert "private_script_value" not in job["description"]
    assert "<" not in job["description"]
    assert job["remote_type"] == "REMOTE"
    assert job["location"] == "Berlin, Germany"
    attrs = job["attributes"]
    assert attrs["city"] == "Berlin"
    assert attrs["provider"] == "arbeitnow"
    assert attrs["tenant"] == "default"
    assert attrs["lifecycle_claim"] == "POSSIBLY_ACTIVE"
    assert attrs["remote_regions"] == []
    assert attrs["country"] is None  # "us" in the description is not the United States.
    assert attrs["remote_indicated"] is True
    assert attrs["employment_type"] == "FULL_TIME"
    assert attrs["salary_min"] is None and attrs["salary_max"] is None
    assert attrs["salary_currency"] is None and attrs["salary_source"] == "UNKNOWN"
    assert attrs["reported_posted_at"] == "2026-11-12T12:00:00+00:00"
    assert attrs["submission_capability"] == "MANUAL_ONLY"
    assert attrs["source_attribution"] == {"name": "Arbeitnow", "url": "https://www.arbeitnow.com"}


def test_false_remote_is_not_automatically_onsite_and_explicit_hybrid_is_retained(payload):
    raw = payload["data"][1]
    job = Arbeitnow().normalize(raw)
    assert job["remote_type"] == "HYBRID"
    assert job["attributes"]["remote_indicated"] is False
    assert job["attributes"]["remote_regions"] == []
    assert job["attributes"]["country"] == "GB"
    raw["location"] = "London, United Kingdom"
    assert Arbeitnow().normalize(raw)["remote_type"] == "UNKNOWN"


@pytest.mark.parametrize("location", ("", "Remote", "Berlin", None))
def test_remote_without_explicit_geography_never_means_worldwide_or_us(payload, location):
    raw = dict(payload["data"][0], location=location, remote=True)
    job = Arbeitnow().normalize(raw)
    assert job["remote_type"] == "REMOTE"
    assert job["attributes"]["remote_regions"] == []
    assert job["attributes"]["country"] is None


@pytest.mark.parametrize("remote", (None, "true", "false", 1, 0, [], {}))
def test_malformed_optional_remote_flag_stays_unknown(payload, remote):
    raw = dict(payload["data"][0], remote=remote, location="Berlin")
    job = Arbeitnow().normalize(raw)
    assert job["remote_type"] == "UNKNOWN"
    assert job["attributes"]["remote_indicated"] is None
    assert job["attributes"]["remote_regions"] == []


@pytest.mark.parametrize("created_at", (None, True, False, "tomorrow", {}, [], 0, -1,
                                       float("nan"), float("inf"), 10**400, -(10**400)))
def test_malformed_optional_posted_date_is_unknown(payload, created_at):
    raw = dict(payload["data"][0], created_at=created_at)
    assert Arbeitnow().normalize(raw)["attributes"]["reported_posted_at"] is None


def test_posted_date_accepts_finite_positive_epoch(payload):
    raw = dict(payload["data"][0], created_at=1234567890.5)
    assert Arbeitnow().normalize(raw)["attributes"]["reported_posted_at"] == (
        datetime.fromtimestamp(1234567890.5, tz=timezone.utc).isoformat())


@pytest.mark.parametrize("host", sorted(LISTING_HOSTS))
def test_explicit_regional_listing_origins_preserve_url_and_native_identity(payload, host):
    raw = deepcopy(payload["data"][0])
    raw["url"] = raw["url"].replace("www.arbeitnow.com", host)
    company = company_identity(raw)
    assert company.identity_parts == ("arbeitnow", host, "synthetic-acme-cloud")
    assert Arbeitnow().normalize(raw)["application_url"] == raw["url"]


def test_company_display_name_does_not_determine_provider_identity(payload):
    raw = payload["data"][0]
    renamed = dict(raw, company_name="A different display label")
    assert company_identity(raw).identity_parts == company_identity(renamed).identity_parts
    other_region = dict(raw, url=raw["url"].replace("www.arbeitnow.com", "www.arbeitnow.nl"))
    assert company_identity(raw).identity_parts != company_identity(other_region).identity_parts


@pytest.mark.parametrize("url", (
    "http://www.arbeitnow.com/jobs/companies/synthetic-acme-cloud/senior-python-engineer-berlin-1001",
    "https://www.arbeitnow.com.attacker.invalid/jobs/companies/synthetic-acme-cloud/senior-python-engineer-berlin-1001",
    "https://www.arbeitnow.com:443/jobs/companies/synthetic-acme-cloud/senior-python-engineer-berlin-1001",
    "https://user@www.arbeitnow.com/jobs/companies/synthetic-acme-cloud/senior-python-engineer-berlin-1001",
    "https://@www.arbeitnow.com/jobs/companies/synthetic-acme-cloud/senior-python-engineer-berlin-1001",
    "https://www.arbeitnow.com/jobs/companies/synthetic-acme-cloud/another-job",
    "https://www.arbeitnow.com/jobs/companies/../senior-python-engineer-berlin-1001",
    "https://www.arbeitnow.com/jobs/companies/synthetic-acme-cloud/senior-python-engineer-berlin-1001?redirect=https://attacker.invalid",
    "https://www.arbeitnow.com/jobs/companies/synthetic-acme-cloud/senior-python-engineer-berlin-1001#fragment",
    "https://www.arbeitnow.com/jobs/companies/synthetic-acme-cloud/senior-python-engineer-berlin-1001//",
    "https://www.arbeitnow.com\\@attacker.invalid/jobs/companies/synthetic-acme-cloud/senior-python-engineer-berlin-1001",
    "https://www.arbeitnow.com/jobs/companies/synthetic-acme-cloud/%73enior-python-engineer-berlin-1001",
    "https://[broken",
    "", None, {}, [],
))
def test_unproven_required_listing_identity_is_rejected_without_rewriting(payload, url):
    raw = dict(payload["data"][0], url=url)
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        company_identity(raw)
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        Arbeitnow().normalize(raw)


@pytest.mark.parametrize(("field", "value"), (
    ("slug", ""), ("slug", None), ("slug", True), ("slug", "UPPER"),
    ("slug", "a" * 513), ("company_name", ""), ("company_name", {}),
))
def test_required_identity_defects_fail_batch_instead_of_discarding_rows(payload, field, value):
    payload["data"][1][field] = value
    adapter = adapter_for(payload)
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        adapter.search()
    assert adapter.health() == "SOURCE_INVALID_JOB"


@pytest.mark.parametrize(("field", "value"), (("title", ""), ("title", []), ("description", {})))
def test_invalid_required_normalization_field_is_rejected(payload, field, value):
    raw = dict(payload["data"][0], **{field: value})
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        Arbeitnow().normalize(raw)


def test_response_links_and_embedded_urls_are_never_fetched(payload):
    payload["links"]["next"] = "http://127.0.0.1/admin"
    payload["data"][0]["company_logo"] = "http://169.254.169.254/latest/meta-data/"
    payload["data"][0]["description"] += '<img src="https://attacker.invalid/pixel">'
    requests = []
    adapter_for(payload, requests).search()
    assert [str(request.url) for request in requests] == [API_URL]


@pytest.mark.parametrize(("status", "headers", "code", "retry"), (
    (302, {"location": "http://127.0.0.1/admin"}, "SOURCE_REDIRECT_BLOCKED", 60),
    (401, {}, "SOURCE_UNAVAILABLE", 300),
    (403, {}, "SOURCE_UNAVAILABLE", 300),
    (429, {"retry-after": "89"}, "SOURCE_RATE_LIMITED", 89),
    (503, {"retry-after": "600"}, "SOURCE_SERVER_ERROR", 600),
))
def test_http_failures_are_bounded_and_report_health(status, headers, code, retry):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, text="private provider diagnostics")

    adapter = Arbeitnow(transport=httpx.MockTransport(respond))
    with pytest.raises(SourceError) as failure:
        adapter.search()
    assert failure.value.code == code
    assert failure.value.retry_seconds == retry
    assert adapter.health() == code
    assert len(requests) == 1
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("content", (b"not JSON", b'{"data":[NaN]}', b"\xff", b'{"other":[]}',
                                     b'{"data":[],"extra":1e999}', b'{"data":[],"extra":-1e999}',
                                     b"[" * 20000 + b"]" * 20000),
                         ids=("invalid_json", "nan", "invalid_utf8", "missing_data",
                              "overflow_positive", "overflow_negative", "deep_nesting"))
def test_invalid_payload_is_rejected(content):
    adapter = Arbeitnow(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=content, headers={"content-type": "application/json"})))
    with pytest.raises(SourceError, match="^SOURCE_PAYLOAD_INVALID$"):
        adapter.search()
    assert adapter.health() == "SOURCE_PAYLOAD_INVALID"


@pytest.mark.parametrize("payload_value", ([], None, {"data": {}}, {"data": [None]}))
def test_incompatible_payload_shapes_never_reach_persistence(payload_value):
    adapter = adapter_for(payload_value)
    with pytest.raises(SourceError, match="^SOURCE_(PAYLOAD_INVALID|INVALID_JOB)$"):
        adapter.search()


def test_wrong_content_type_is_rejected_before_parsing():
    adapter = Arbeitnow(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, text='{"data":[]}', headers={"content-type": "text/html"})))
    with pytest.raises(SourceError, match="^SOURCE_PAYLOAD_INVALID$"):
        adapter.search()


def test_payload_size_limit_closes_stream_before_full_consumption():
    closed = []
    yielded = []

    class LargeStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(100):
                yielded.append(index)
                yield b"x" * 65536

        def close(self):
            closed.append(True)

    adapter = Arbeitnow(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=LargeStream(),
                                       headers={"content-type": "application/json"})))
    with pytest.raises(SourceError, match="^SOURCE_PAYLOAD_LIMIT$"):
        adapter.search()
    assert len(yielded) == MAX_RESPONSE_BYTES // 65536 + 1
    assert len(yielded) < 100
    assert closed
    assert adapter.health() == "SOURCE_PAYLOAD_LIMIT"


def test_record_count_is_bounded_before_identity_expansion(payload):
    payload["data"] = [payload["data"][0]] * (MAX_PAGE_ITEMS + 1)
    adapter = adapter_for(payload)
    with pytest.raises(SourceError, match="^SOURCE_PAYLOAD_INVALID$"):
        adapter.search()


def test_operation_cancellation_during_stream_preserves_exception_and_closes_response(payload):
    cancelled = []
    closed = []
    stop = OperationCancelled("synthetic cancellation")

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"data":'
            cancelled.append(True)
            yield json.dumps(payload["data"]).encode() + b"}"

        def close(self):
            closed.append(True)

    def check():
        if cancelled:
            raise stop

    adapter = Arbeitnow(check=check, transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=Stream(), headers={"content-type": "application/json"})))
    with pytest.raises(OperationCancelled) as failure:
        adapter.search()
    assert failure.value is stop
    assert closed
    assert adapter.health() == "NOT_CHECKED"
    # The exact same adapter can be used after cancellation; its slot was released.
    adapter.check = lambda: None
    adapter.transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    assert len(adapter.search().items) == 2
    assert adapter.health() == "HEALTHY"


@pytest.mark.parametrize("stage", ("decode", "last_identity"))
@pytest.mark.parametrize("control_error", (OperationCancelled, OperationTimedOut))
def test_cooperative_stop_after_decode_or_identity_preserves_previous_health(
        payload, stage, control_error):
    stopped = []
    stop = control_error("synthetic control outcome")
    real_decode = json.loads

    def check():
        if stopped:
            raise stop

    def decode(*args, **kwargs):
        result = real_decode(*args, **kwargs)
        stopped.append(True)
        return result

    def identify(raw):
        result = company_identity(raw)
        if raw["slug"] == payload["data"][-1]["slug"]:
            stopped.append(True)
        return result

    adapter = adapter_for(payload, check=check)
    assert len(adapter.search().items) == 2
    assert adapter.health() == "HEALTHY"
    target, replacement = (
        ("services.discovery.sources.arbeitnow.json.loads", decode) if stage == "decode"
        else ("services.discovery.sources.arbeitnow.company_identity", identify))
    with patch(target, side_effect=replacement):
        with pytest.raises(control_error) as failure:
            adapter.search()
    assert failure.value is stop
    assert adapter.health() == "HEALTHY"


def test_decode_time_is_charged_to_request_deadline(payload):
    instant = [0.0]
    real_decode = json.loads

    def decode(*args, **kwargs):
        result = real_decode(*args, **kwargs)
        instant[0] = 11.0
        return result

    adapter = adapter_for(payload, request_timeout=10)
    with patch("services.discovery.sources.arbeitnow.time.monotonic", side_effect=lambda: instant[0]), \
            patch("services.discovery.sources.arbeitnow.json.loads", side_effect=decode):
        with pytest.raises(SourceError, match="^SOURCE_TIME_LIMIT$"):
            adapter.search()
    assert adapter.health() == "SOURCE_TIME_LIMIT"


def test_exhausted_operation_budget_stops_before_network(payload):
    class Check:
        def __call__(self):
            pass

        def remaining_seconds(self):
            return 0

    requests = []
    adapter = adapter_for(payload, requests, check=Check())
    with pytest.raises(SourceError, match="^SOURCE_TIME_LIMIT$"):
        adapter.search()
    assert requests == []


@pytest.mark.parametrize(("error", "code"), (
    (httpx.ReadTimeout("private diagnostic"), "SOURCE_TIMEOUT"),
    (httpx.ReadError("private diagnostic"), "SOURCE_NETWORK_ERROR"),
))
def test_transport_failures_hide_raw_diagnostics(error, code):
    def fail(_request):
        raise error

    adapter = Arbeitnow(transport=httpx.MockTransport(fail))
    with pytest.raises(SourceError) as failure:
        adapter.search()
    assert str(failure.value) == code
    assert adapter.health() == code


def test_transport_exception_does_not_replace_native_operation_cancellation():
    cancelled = []
    stop = OperationCancelled("synthetic control outcome")

    def fail(_request):
        cancelled.append(True)
        raise httpx.ReadTimeout("private diagnostic")

    def check():
        if cancelled:
            raise stop

    adapter = Arbeitnow(transport=httpx.MockTransport(fail), check=check)
    with pytest.raises(OperationCancelled) as failure:
        adapter.search()
    assert failure.value is stop
    assert adapter.health() == "NOT_CHECKED"


def test_rate_wait_respects_schedule_and_cancellation_without_wall_clock_sleep():
    instant = [0.0]
    sleeps = []
    cancelled = []
    stop = OperationCancelled("synthetic throttle cancellation")

    def sleep(seconds):
        sleeps.append(seconds)
        instant[0] += seconds

    def check():
        if cancelled:
            raise stop

    adapter = Arbeitnow(rate_limit=600, check=check)
    with patch("services.discovery.sources.arbeitnow.time.monotonic", side_effect=lambda: instant[0]), \
            patch("services.discovery.sources.arbeitnow.time.sleep", side_effect=sleep):
        adapter._wait_for_rate_limit()
        adapter._wait_for_rate_limit()
        assert instant[0] == pytest.approx(0.1)
        assert all(delay <= 0.05 for delay in sleeps)
        cancelled.append(True)
        with pytest.raises(OperationCancelled) as failure:
            adapter._wait_for_rate_limit()
        assert failure.value is stop


def test_configured_request_concurrency_limits_inflight_fixture_requests(payload):
    first_entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0
    failures = []
    batches = []

    def respond(_request):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        first_entered.set()
        try:
            if not release.wait(2):
                raise AssertionError("Fixture was not released")
            return httpx.Response(200, json=payload)
        finally:
            with lock:
                active -= 1

    adapter = Arbeitnow(concurrency=1, transport=httpx.MockTransport(respond))

    def run(second=False):
        if second:
            second_started.set()
        try:
            batches.append(adapter.search())
        except BaseException as error:
            failures.append(error)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run, args=(True,))
    first.start()
    try:
        assert first_entered.wait(1)
        second.start()
        assert second_started.wait(1)
        with lock:
            assert active == 1
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert len(batches) == 2
    assert maximum == 1
