"""Synthetic USAJOBS contracts; no live requests or external messages."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from unittest.mock import patch

import httpx
import pytest

from services.discovery.sources import usajobs as source
from services.discovery.sources.contract import CollectionStrategy, SourceCapability
from services.discovery.sources.errors import SourceError
from workers.operations import OperationCancelled, OperationTimedOut

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


@pytest.fixture
def payloads():
    root = Path(__file__).resolve().parents[1] / "fixtures"
    return tuple(json.loads((root / name).read_text(encoding="utf-8")) for name in (
        "usajobs_metadata.json", "usajobs_announcement_text.json"))


def response_for(request, payloads):
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "data.usajobs.gov"
    if request.url.path == "/api/historicjoa":
        return httpx.Response(200, json=payloads[0])
    assert request.url.path == "/api/historicjoa/announcementtext"
    return httpx.Response(200, json=payloads[1])


def adapter_for(payloads, requests=None, handler=None, **kwargs):
    requests = [] if requests is None else requests

    def respond(request):
        requests.append(request)
        return handler(request) if handler else response_for(request, payloads)

    kwargs.setdefault("clock", lambda: NOW)
    return source.USAJobs(transport=httpx.MockTransport(respond), **kwargs)


def envelope(payloads, index=0):
    return {"metadata": deepcopy(payloads[0]["data"][index]),
            "announcement_text": deepcopy(payloads[1]["data"][index])}


def single(payloads):
    return ({"data": [deepcopy(payloads[0]["data"][0])]},
            {"data": [deepcopy(payloads[1]["data"][0])]})


def fail_response(code=503):
    return httpx.Response(code, text="private provider diagnostic")


def test_search_uses_two_fixed_gets_exact_window_and_preserved_raw(payloads):
    requests = []
    before = deepcopy(payloads)
    adapter = adapter_for(payloads, requests)
    assert adapter.health() == "NOT_CHECKED"
    batch = adapter.search()
    assert len(requests) == 2
    assert str(requests[0].url).partition("?")[0] == source.METADATA_URL
    assert dict(requests[0].url.params) == {
        "PositionSeries": "2210", "StartPositionOpenDate": "11-07-2026",
        "EndPositionOpenDate": "11-13-2026",
    }
    assert str(requests[1].url).partition("?")[0] == source.ANNOUNCEMENT_TEXT_URL
    assert dict(requests[1].url.params) == {"USAJOBSControlNumbers": "990000103,990000101,990000102"}
    assert all(request.headers["accept"] == "application/json" for request in requests)
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)
    assert all("authorization" not in request.headers and "authorization-key" not in request.headers
               for request in requests)
    assert batch.endpoint == source.METADATA_URL
    assert batch.source_company is None and batch.complete_inventory is False
    assert [item.raw["metadata"]["usajobsControlNumber"] for item in batch.items] == [
        990000103, 990000101, 990000102]
    for item in batch.items:
        control = item.raw["metadata"]["usajobsControlNumber"]
        assert item.raw["metadata"] == next(row for row in before[0]["data"]
                                           if row["usajobsControlNumber"] == control)
        assert item.raw["announcement_text"] == next(row for row in before[1]["data"]
                                                    if row["usajobsControlNumber"] == control)
    assert payloads == before
    assert adapter.health() == "HEALTHY"


def test_capabilities_and_unsupported_detail_fetch_do_not_imply_submission(payloads):
    requests = []
    adapter = adapter_for(payloads, requests)
    assert adapter.capabilities().strategy == CollectionStrategy.PUBLIC_JSON
    assert adapter.capabilities().features == frozenset({
        SourceCapability.DISCOVERY, SourceCapability.APPLICATION_REDIRECT})
    for identifier in (990000101, "https://attacker.invalid", None):
        with pytest.raises(SourceError, match="^SOURCE_UNSUPPORTED$"):
            adapter.fetch(identifier)
    assert requests == [] and adapter.health() == "NOT_CHECKED"


@pytest.mark.parametrize(("name", "value", "code"), [
    ("tenant", "", "TENANT"), ("tenant", "2210", "TENANT"),
    ("tenant", "https://attacker.invalid", "TENANT"), ("tenant", None, "TENANT"),
    ("request_timeout", True, "REQUEST_TIMEOUT"), ("request_timeout", 1, "REQUEST_TIMEOUT"),
    ("request_timeout", 61, "REQUEST_TIMEOUT"), ("request_timeout", "10", "REQUEST_TIMEOUT"),
    ("rate_limit", False, "RATE_LIMIT"), ("rate_limit", 0, "RATE_LIMIT"),
    ("rate_limit", 601, "RATE_LIMIT"), ("rate_limit", "60", "RATE_LIMIT"),
    ("concurrency", True, "CONCURRENCY"), ("concurrency", 0, "CONCURRENCY"),
    ("concurrency", 9, "CONCURRENCY"), ("clock", NOW, "CLOCK"),
])
def test_invalid_configuration_rejected_before_egress(name, value, code):
    with pytest.raises(ValueError, match=f"^SOURCE_INVALID_{code}$"):
        source.USAJobs(**{name: value})


@pytest.mark.parametrize("instant", (None, "2026-11-13", datetime(2026, 11, 13),
                                    datetime.min.replace(tzinfo=timezone.utc)))
def test_invalid_clock_stops_before_egress(payloads, instant):
    requests = []
    adapter = adapter_for(payloads, requests, clock=lambda: instant)
    with pytest.raises(ValueError, match="^SOURCE_INVALID_CLOCK$"):
        adapter.search()
    assert requests == [] and adapter.health() == "NOT_CHECKED"


def test_window_is_seven_inclusive_utc_dates_not_local_calendar_dates():
    requests = []
    local_time = datetime(2026, 11, 14, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    adapter = adapter_for(({"data": []}, {"data": []}), requests, clock=lambda: local_time)
    assert adapter.search().items == ()
    assert dict(requests[0].url.params) == {
        "PositionSeries": "2210", "StartPositionOpenDate": "11-07-2026",
        "EndPositionOpenDate": "11-13-2026"}
    assert len(requests) == 1 and adapter.health() == "HEALTHY"


@pytest.mark.parametrize("case", ("empty", "before_window", "after_window", "other_series"))
def test_no_selected_metadata_never_requests_announcement_text(payloads, case):
    metadata, text = single(payloads)
    if case == "empty":
        metadata["data"] = []
    elif case == "other_series":
        metadata["data"][0]["jobcategories"] = [{"series": "0996"}]
    else:
        metadata["data"][0]["positionOpenDate"] = (
            "2026-11-06" if case == "before_window" else "2026-11-14")
    requests = []
    adapter = adapter_for((metadata, text), requests)
    assert adapter.search().items == ()
    assert len(requests) == 1 and adapter.health() == "HEALTHY"


def test_selection_is_newest_first_bounded_and_text_order_does_not_join_by_index(payloads):
    metadata_rows, text_rows = [], []
    for index in range(35):
        raw = envelope(payloads)
        for record in raw.values():
            record.update(usajobsControlNumber=990001000 + index,
                          announcementNumber=f"SYN-IT-{index}",
                          positionOpenDate=(NOW.date() - timedelta(days=index % 7)).isoformat())
        metadata_rows.append(raw["metadata"])
        text_rows.append(raw["announcement_text"])
    expected = sorted(metadata_rows, key=lambda row: (
        row["positionOpenDate"], row["usajobsControlNumber"]), reverse=True)[:25]
    requests = []

    def respond(request):
        if request.url.path == "/api/historicjoa":
            return httpx.Response(200, json={"data": list(reversed(metadata_rows))})
        controls = request.url.params["USAJOBSControlNumbers"].split(",")
        assert len(controls) == 25 and len(set(controls)) == 25
        return httpx.Response(200, json={"data": [
            row for row in reversed(text_rows) if str(row["usajobsControlNumber"]) in controls]})

    batch = adapter_for(payloads, requests, handler=respond).search()
    assert len(requests) == 2 and len(batch.items) == source.MAX_SELECTED == 25
    assert [item.raw["metadata"] for item in batch.items] == expected
    for item in batch.items:
        assert item.raw["metadata"]["usajobsControlNumber"] == item.raw["announcement_text"]["usajobsControlNumber"]
    assert batch.complete_inventory is False


def selection_boundary_payloads(payloads):
    """Twenty-five supported rows plus one older synthetic agency-label defect."""
    metadata_rows, text_rows = [], []
    for index in range(26):
        raw = envelope(payloads)
        opened = NOW.date() - timedelta(days=int(index == 25))
        for record in raw.values():
            record.update(usajobsControlNumber=990002000 + index,
                          announcementNumber=f"SYN-SELECTION-{index}",
                          positionOpenDate=opened.isoformat())
        metadata_rows.append(raw["metadata"])
        text_rows.append(raw["announcement_text"])
    metadata_rows[-1]["hiringAgencyCode"] = "Other Agencies and Independent Organizations"
    return {"data": metadata_rows}, {"data": text_rows}


@pytest.mark.parametrize("order", ("forward", "reverse", "rotated"))
def test_unselected_agency_label_does_not_poison_deterministic_newest_25(payloads, order):
    metadata, text = selection_boundary_payloads(payloads)
    if order == "reverse":
        metadata["data"].reverse()
    elif order == "rotated":
        metadata["data"] = metadata["data"][7:] + metadata["data"][:7]
    before = deepcopy((metadata, text))
    expected = list(range(990002024, 990001999, -1))
    requests = []

    def respond(request):
        if request.url.path == "/api/historicjoa":
            return response_for(request, (metadata, text))
        assert request.url.path == "/api/historicjoa/announcementtext"
        assert dict(request.url.params) == {
            "USAJOBSControlNumbers": ",".join(map(str, expected))}
        return httpx.Response(200, json={"data": [
            row for row in reversed(text["data"])
            if row["usajobsControlNumber"] in expected]})

    adapter = adapter_for((metadata, text), requests, handler=respond)
    batch = adapter.search()
    assert len(requests) == 2
    assert [item.raw["metadata"]["usajobsControlNumber"] for item in batch.items] == expected
    native = payloads[0]["data"][0]
    assert all(item.company.identity_parts == (
        "usajobs", native["hiringDepartmentCode"], native["hiringAgencyCode"])
        for item in batch.items)
    assert batch.complete_inventory is False and adapter.health() == "HEALTHY"
    assert (metadata, text) == before


@pytest.mark.parametrize(("field", "value"), (
    ("hiringAgencyCode", "Other Agencies and Independent Organizations"),
    ("hiringDepartmentCode", "Unsupported department label"),
    ("announcementNumber", " "),
))
def test_selected_identity_defect_fails_before_text_without_backfill(payloads, field, value):
    metadata, text = selection_boundary_payloads(payloads)
    newest = metadata["data"][-1]
    newest["hiringAgencyCode"] = payloads[0]["data"][0]["hiringAgencyCode"]
    newest["positionOpenDate"] = NOW.date().isoformat()
    newest[field] = value
    requests = []
    adapter = adapter_for((metadata, text), requests)
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        adapter.search()
    assert len(requests) == 1
    assert requests[0].url.path == "/api/historicjoa"
    assert adapter.health() == "SOURCE_INVALID_JOB"


@pytest.mark.parametrize(("field", "value"), (
    ("usajobsControlNumber", True),
    ("positionOpenDate", "2026-11-99"),
))
def test_unselected_row_still_requires_valid_selection_keys(payloads, field, value):
    metadata, text = selection_boundary_payloads(payloads)
    metadata["data"][-1][field] = value
    requests = []
    adapter = adapter_for((metadata, text), requests)
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        adapter.search()
    assert len(requests) == 1 and adapter.health() == "SOURCE_INVALID_JOB"


def test_unselected_identity_defect_cannot_hide_conflicting_duplicate(payloads):
    metadata, text = selection_boundary_payloads(payloads)
    conflicting = deepcopy(metadata["data"][-1])
    conflicting["positionTitle"] = "Different synthetic source claim"
    metadata["data"].append(conflicting)
    requests = []
    adapter = adapter_for((metadata, text), requests)
    with pytest.raises(SourceError, match="^SOURCE_JOIN_CONFLICT$"):
        adapter.search()
    assert len(requests) == 1 and adapter.health() == "SOURCE_JOIN_CONFLICT"


@pytest.mark.parametrize("phase", ("metadata", "text"))
def test_identical_duplicate_rows_are_consistent_and_returned_once(payloads, phase):
    metadata, text = single(payloads)
    selected = metadata if phase == "metadata" else text
    selected["data"].append(deepcopy(selected["data"][0]))
    batch = adapter_for((metadata, text)).search()
    assert len(batch.items) == 1


@pytest.mark.parametrize("phase", ("metadata", "text"))
def test_conflicting_duplicates_fail_entire_search(payloads, phase):
    metadata, text = single(payloads)
    selected = metadata if phase == "metadata" else text
    duplicate = deepcopy(selected["data"][0])
    duplicate["positionTitle" if phase == "metadata" else "summary"] = "Conflicting source claim"
    selected["data"].append(duplicate)
    requests = []
    adapter = adapter_for((metadata, text), requests)
    with pytest.raises(SourceError, match="^SOURCE_JOIN_CONFLICT$"):
        adapter.search()
    assert adapter.health() == "SOURCE_JOIN_CONFLICT"
    assert len(requests) == (1 if phase == "metadata" else 2)


def test_one_control_number_with_two_openings_is_ambiguous_not_a_guessed_newest_join(payloads):
    metadata, text = single(payloads)
    duplicate = dict(metadata["data"][0], positionOpenDate="2026-11-13")
    metadata["data"].append(duplicate)
    with pytest.raises(SourceError, match="^SOURCE_JOIN_CONFLICT$"):
        adapter_for((metadata, text)).search()


@pytest.mark.parametrize(("field", "value"), (
    ("hiringAgencyCode", "OTHER"), ("hiringDepartmentCode", "OTHER"),
    ("positionOpenDate", "2026-11-11"), ("announcementNumber", "ANOTHER-ANNOUNCEMENT"),
    ("usajobsControlNumber", 990099999),
))
def test_text_identity_mismatch_and_extras_are_rejected(payloads, field, value):
    metadata, text = single(payloads)
    text["data"][0][field] = value
    adapter = adapter_for((metadata, text))
    with pytest.raises(SourceError, match="^SOURCE_JOIN_MISMATCH$"):
        adapter.search()
    assert adapter.health() == "SOURCE_JOIN_MISMATCH"


def test_missing_text_prevents_partial_result(payloads):
    payloads[1]["data"].pop()
    adapter = adapter_for(payloads)
    with pytest.raises(SourceError, match="^SOURCE_JOIN_MISSING$"):
        adapter.search()
    assert adapter.health() == "SOURCE_JOIN_MISSING"


@pytest.mark.parametrize("control", (None, True, 0, -1, 1.5, "990000101", 2**63))
def test_control_numbers_must_be_positive_bounded_native_integers(payloads, control):
    metadata, text = single(payloads)
    metadata["data"][0]["usajobsControlNumber"] = control
    requests = []
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        adapter_for((metadata, text), requests).search()
    assert len(requests) == 1


@pytest.mark.parametrize(("field", "value"), (
    ("hiringAgencyCode", ""), ("hiringAgencyCode", "aa"), ("hiringAgencyCode", "AA/BB"),
    ("hiringDepartmentCode", None), ("announcementNumber", ""), ("announcementNumber", "A\nB"),
    ("positionOpenDate", "2026-02-30"), ("positionOpenDate", "11/12/2026"),
))
def test_malformed_required_metadata_identity_is_not_guessed(payloads, field, value):
    metadata, text = single(payloads)
    metadata["data"][0][field] = value
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        adapter_for((metadata, text)).search()


@pytest.mark.parametrize(("lower", "documented"), source._COLLECTIONS)
def test_equal_case_aliases_are_allowed_but_conflicting_aliases_fail(payloads, lower, documented):
    metadata, text = single(payloads)
    row = metadata["data"][0]
    row[documented] = deepcopy(row[lower])
    assert len(adapter_for((metadata, text)).search().items) == 1
    row[documented] = [{"conflicting": "value"}]
    with pytest.raises(SourceError, match="^SOURCE_ALIAS_CONFLICT$"):
        adapter_for((metadata, text)).search()


def test_nested_location_alias_conflicts_are_not_resolved_by_order(payloads):
    raw = envelope(payloads)
    raw["metadata"]["positionlocations"][0]["positionlocationcity"] = "Austin"
    with pytest.raises(SourceError, match="^SOURCE_ALIAS_CONFLICT$"):
        source.USAJobs().normalize(raw)


def test_agency_names_never_replace_stable_native_code_identity(payloads):
    first = envelope(payloads, 0)
    changed = deepcopy(first)
    changed["metadata"]["hiringAgencyName"] = None
    company = source.company_identity(first)
    assert company.identity_parts == ("usajobs", "ZZ", "ZZIT")
    assert company.name == "USAJOBS agency ZZIT"
    assert source.company_identity(changed) == company
    assert source.company_identity(envelope(payloads, 2)) == company
    assert source.company_identity(envelope(payloads, 1)).identity_parts != company.identity_parts


def test_normalization_preserves_raw_and_conservative_provider_facts(payloads):
    raw = envelope(payloads)
    original = deepcopy(raw)
    job = source.USAJobs().normalize(raw)
    assert raw == original
    assert job["requisition"] == "990000101"
    assert job["application_url"] == "https://www.usajobs.gov/job/990000101"
    assert job["title"] == raw["metadata"]["positionTitle"]
    assert "private_script_value" not in job["description"]
    assert "<" not in job["description"]
    assert "Python" in job["skills"]
    assert job["location"] == "Dallas, TX, United States"
    assert job["remote_type"] == "UNKNOWN"
    attrs = job["attributes"]
    assert attrs["company_name"] == "USAJOBS agency ZZIT"
    assert attrs["source_agency_name"] == "Synthetic Technology Agency"
    assert attrs["country"] == "US" and attrs["city"] == "Dallas" and attrs["state"] == "TX"
    assert attrs["telework_eligible_raw"] == "Y"
    assert attrs["remote_indicated"] is None and attrs["remote_regions"] == []
    assert attrs["salary_min"] is None and attrs["salary_max"] is None
    assert attrs["salary_currency"] is None and attrs["salary_source"] == "UNKNOWN"
    assert attrs["source_salary_claim"]["minimumSalary"] == 90000
    assert attrs["employment_type"] == "FULL_TIME"
    assert attrs["reported_posted_at"] is None and attrs["reported_posted_at_raw"] is None
    assert attrs["source_open_date"] == "2026-11-12"
    assert attrs["source_open_date_precision"] == "DATE" and attrs["source_date_timezone"] is None
    assert attrs["application_url_status"] == "CONSTRUCTED_UNVERIFIED"
    assert attrs["route_confirmation"] == "NOT_RUN"
    assert attrs["submission_capability"] == "MANUAL_ONLY"
    assert attrs["source_attribution"] == {"name": "USAJOBS", "url": source.ATTRIBUTION_URL, "unofficial": True}
    assert attrs["source_endpoints"] == [source.METADATA_URL, source.ANNOUNCEMENT_TEXT_URL]


@pytest.mark.parametrize(("status", "expected"), (
    (None, "UNKNOWN"), ("", "UNKNOWN"), ("unexpected status", "UNKNOWN"),
    (True, "UNKNOWN"), ({"status": "Accepting applications"}, "UNKNOWN"),
    ("Accepting applications", "POSSIBLY_ACTIVE"),
    ("Applications under review", "CLOSED"), ("Reviewing applications", "CLOSED"),
    ("Job closed", "CLOSED"), ("Job canceled", "CLOSED"),
    ("Position filled", "FILLED"), ("Hiring complete", "UNKNOWN"),
))
def test_only_explicit_structured_statuses_support_lifecycle_claims(payloads, status, expected):
    raw = envelope(payloads)
    raw["metadata"]["positionOpeningStatus"] = status
    raw["metadata"]["positionCloseDate"] = "2020-01-01"
    raw["metadata"]["positionExpireDate"] = "2020-01-01"
    attrs = source.USAJobs().normalize(raw)["attributes"]
    assert attrs["lifecycle_claim"] == expected
    assert attrs["source_opening_status"] == status
    assert attrs["source_close_date"] == "2020-01-01"
    assert attrs["source_expire_date"] == "2020-01-01"


def test_invalid_optional_dates_remain_native_evidence_without_inventing_timestamps(payloads):
    raw = envelope(payloads)
    raw["metadata"]["positionCloseDate"] = "not a date"
    raw["metadata"]["positionExpireDate"] = 10**100
    attrs = source.USAJobs().normalize(raw)["attributes"]
    assert attrs["source_close_date"] == "not a date"
    assert attrs["source_expire_date"] == 10**100
    assert attrs["source_close_date_precision"] == attrs["source_expire_date_precision"] == "UNKNOWN"
    assert attrs["reported_posted_at"] is None
    assert attrs["lifecycle_claim"] == "POSSIBLY_ACTIVE"


def test_country_and_city_come_from_explicit_location_not_provider_or_prose(payloads):
    raw = envelope(payloads, 2)
    job = source.USAJobs().normalize(raw)
    assert job["attributes"]["country"] == "CA"
    assert job["attributes"]["city"] == "Ottawa"
    assert job["attributes"]["remote_regions"] == []
    del raw["metadata"]["positionLocations"]
    job = source.USAJobs().normalize(raw)
    assert job["location"] == "Unknown"
    assert job["attributes"]["country"] is None and job["attributes"]["city"] is None
    assert job["remote_type"] == "UNKNOWN"


def test_multiple_locations_are_preserved_without_arbitrarily_picking_a_country(payloads):
    raw = envelope(payloads)
    raw["metadata"]["positionlocations"].append({
        "positionLocationCity": "Ottawa", "positionLocationState": "Ontario",
        "positionLocationCountry": "Canada"})
    job = source.USAJobs().normalize(raw)
    assert "Dallas" in job["location"] and "Ottawa" in job["location"]
    assert len(job["attributes"]["source_locations"]) == 2
    assert job["attributes"]["country"] is None and job["attributes"]["city"] is None


@pytest.mark.parametrize(("section", "field", "value"), (
    ("metadata", "positionTitle", ""), ("metadata", "positionTitle", []),
    ("announcement_text", "summary", {}), ("announcement_text", "requirementsQualifications", 15),
    ("metadata", "unrecognized", float("nan")), ("metadata", "unrecognized", "\ud800"),
))
def test_invalid_normalization_data_is_rejected_without_mutating_raw(payloads, section, field, value):
    raw = envelope(payloads)
    raw[section][field] = value
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        source.USAJobs().normalize(raw)


def test_missing_all_announcement_text_is_not_fabricated_from_metadata(payloads):
    raw = envelope(payloads)
    for field, _label in source._TEXT_SECTIONS:
        raw["announcement_text"][field] = None
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        source.USAJobs().normalize(raw)


def test_paging_benefits_video_and_html_links_never_choose_network_destinations(payloads):
    payloads[0]["paging"] = {"next": "http://127.0.0.1/admin"}
    payloads[1]["paging"] = {"next": "https://attacker.invalid"}
    for row in payloads[1]["data"]:
        row["benefitsURL"] = "http://169.254.169.254/latest/meta-data/"
        row["videoURL"] = "https://attacker.invalid/pixel"
        row["howToApply"] += '<img src="https://attacker.invalid/pixel">'
    requests = []
    adapter_for(payloads, requests).search()
    assert [request.url.path for request in requests] == [
        "/api/historicjoa", "/api/historicjoa/announcementtext"]
    with pytest.raises(SourceError, match="^SOURCE_INVALID_ENDPOINT$"):
        adapter_for(payloads, requests)._request("http://127.0.0.1/admin", {})
    assert len(requests) == 2


@pytest.mark.parametrize("phase", ("metadata", "text"))
@pytest.mark.parametrize(("status", "headers", "code", "retry"), (
    (302, {"location": "https://attacker.invalid"}, "SOURCE_REDIRECT_BLOCKED", 60),
    (401, {}, "SOURCE_UNAVAILABLE", 300), (403, {}, "SOURCE_UNAVAILABLE", 300),
    (429, {"retry-after": "90"}, "SOURCE_RATE_LIMITED", 90),
    (503, {"retry-after": "600"}, "SOURCE_SERVER_ERROR", 600),
))
def test_http_failures_report_health_and_allow_later_recovery(payloads, phase, status, headers, code, retry):
    requests = []

    def respond(request):
        failing = request.url.path.endswith("/announcementtext") == (phase == "text")
        return httpx.Response(status, headers=headers, text="private diagnostic") if failing else response_for(request, payloads)

    adapter = adapter_for(payloads, requests, handler=respond)
    with pytest.raises(SourceError) as failure:
        adapter.search()
    assert failure.value.code == code and failure.value.retry_seconds == retry
    assert adapter.health() == code
    assert "private" not in str(failure.value)
    assert len(requests) == (1 if phase == "metadata" else 2)
    adapter.transport = httpx.MockTransport(lambda request: response_for(request, payloads))
    assert len(adapter.search().items) == 3 and adapter.health() == "HEALTHY"


@pytest.mark.parametrize("content", (
    b"not JSON", b'{"data":[NaN]}', b'{"data":[Infinity]}',
    b'{"data":[],"extra":1e999}', b'{"data":[],"data":[]}', b"\xff",
    b"[" * 20000 + b"]" * 20000,
), ids=("syntax", "nan", "infinity", "overflow", "duplicate_keys", "utf8", "deep"))
def test_malformed_json_is_a_stable_error(payloads, content):
    adapter = adapter_for(payloads, handler=lambda _request: httpx.Response(
        200, content=content, headers={"content-type": "application/json"}))
    with pytest.raises(SourceError, match="^SOURCE_PAYLOAD_INVALID$"):
        adapter.search()
    assert adapter.health() == "SOURCE_PAYLOAD_INVALID"


@pytest.mark.parametrize("payload_value", (None, [], {"data": None}, {"data": {}}, {"data": [None]}))
def test_bad_envelope_shapes_cannot_produce_jobs(payloads, payload_value):
    adapter = adapter_for(payloads, handler=lambda _request: httpx.Response(200, json=payload_value))
    with pytest.raises(SourceError, match="^SOURCE_(PAYLOAD_INVALID|INVALID_JOB)$"):
        adapter.search()


@pytest.mark.parametrize("headers", ({"content-type": "text/html"},
                                    {"content-type": "application/json", "content-encoding": "gzip"}))
def test_content_type_and_encoding_are_checked_before_consuming_body(payloads, headers):
    class Unreadable(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Rejected body must not be read")
            yield b""

    adapter = adapter_for(payloads, handler=lambda _request: httpx.Response(
        200, stream=Unreadable(), headers=headers))
    with pytest.raises(SourceError, match="^SOURCE_PAYLOAD_INVALID$"):
        adapter.search()


@pytest.mark.parametrize("phase", ("metadata", "text"))
def test_record_limits_apply_before_identity_expansion(payloads, phase):
    chosen = payloads[0 if phase == "metadata" else 1]
    limit = source.MAX_METADATA_ITEMS if phase == "metadata" else source.MAX_SELECTED
    chosen["data"] = [chosen["data"][0]] * (limit + 1)
    adapter = adapter_for(payloads)
    with pytest.raises(SourceError, match="^SOURCE_PAYLOAD_INVALID$"):
        adapter.search()


def test_response_body_limit_closes_stream_without_consuming_rest(payloads):
    yielded, closed = [], []

    class LargeStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(100):
                yielded.append(index)
                yield b"x" * 65536

        def close(self):
            closed.append(True)

    adapter = adapter_for(payloads, handler=lambda _request: httpx.Response(
        200, stream=LargeStream(), headers={"content-type": "application/json"}))
    with pytest.raises(SourceError, match="^SOURCE_PAYLOAD_LIMIT$"):
        adapter.search()
    assert len(yielded) == source.MAX_RESPONSE_BYTES // 65536 + 1
    assert len(yielded) < 100 and closed


@pytest.mark.parametrize("phase", ("metadata", "text"))
@pytest.mark.parametrize("control_error", (OperationCancelled, OperationTimedOut))
def test_cancellation_during_stream_closes_response_releases_slot_and_preserves_health(
        payloads, phase, control_error):
    stopped, closed = [], []
    stop = control_error("synthetic control outcome")

    def check():
        if stopped:
            raise stop

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"data":'
            stopped.append(True)
            yield b"[]}"

        def close(self):
            closed.append(True)

    def respond(request):
        failing = request.url.path.endswith("/announcementtext") == (phase == "text")
        return httpx.Response(200, stream=Stream(), headers={"content-type": "application/json"}) if failing else response_for(request, payloads)

    adapter = adapter_for(payloads, check=check)
    assert len(adapter.search().items) == 3
    adapter.transport = httpx.MockTransport(respond)
    with pytest.raises(control_error) as failure:
        adapter.search()
    assert failure.value is stop and closed and adapter.health() == "HEALTHY"
    stopped.clear()
    adapter.transport = httpx.MockTransport(lambda request: response_for(request, payloads))
    assert len(adapter.search().items) == 3 and adapter.health() == "HEALTHY"


@pytest.mark.parametrize("phase", ("metadata", "text"))
@pytest.mark.parametrize("control_error", (OperationCancelled, OperationTimedOut))
def test_control_outcome_after_decode_is_not_replaced_by_success(payloads, phase, control_error):
    stopped, decoded = [], []
    stop = control_error("synthetic control outcome")
    real_decode = json.loads

    def check():
        if stopped:
            raise stop

    def decode(*args, **kwargs):
        result = real_decode(*args, **kwargs)
        decoded.append(True)
        if len(decoded) == (1 if phase == "metadata" else 2):
            stopped.append(True)
        return result

    adapter = adapter_for(payloads, check=check)
    adapter.search()
    with patch.object(source.json, "loads", side_effect=decode):
        with pytest.raises(control_error) as failure:
            adapter.search()
    assert failure.value is stop and adapter.health() == "HEALTHY"


@pytest.mark.parametrize("phase", ("metadata_identity", "last_item"))
def test_identity_processing_cooperates_before_next_request_and_after_last_item(payloads, phase):
    stopped, calls, requests = [], [], []
    stop = OperationCancelled("synthetic parsing cancellation")
    real_identity, real_company = source._identity, source.company_identity

    def check():
        if stopped:
            raise stop

    def identify(row):
        result = real_identity(row)
        stopped.append(True)
        return result

    def company(raw):
        result = real_company(raw)
        calls.append(True)
        if len(calls) == 3:
            stopped.append(True)
        return result

    adapter = adapter_for(payloads, requests, check=check)
    adapter.search()
    requests.clear()
    name, handler = ("_identity", identify) if phase == "metadata_identity" else ("company_identity", company)
    with patch.object(source, name, side_effect=handler):
        with pytest.raises(OperationCancelled) as failure:
            adapter.search()
    assert failure.value is stop and adapter.health() == "HEALTHY"
    assert len(requests) == (1 if phase == "metadata_identity" else 2)


def test_decode_is_charged_to_monotonic_request_budget(payloads):
    instant = [0.0]
    real_decode = json.loads

    def decode(*args, **kwargs):
        result = real_decode(*args, **kwargs)
        instant[0] = 2.0
        return result

    adapter = adapter_for(payloads, request_timeout=2)
    with patch.object(source.time, "monotonic", side_effect=lambda: instant[0]), \
            patch.object(source.json, "loads", side_effect=decode):
        with pytest.raises(SourceError, match="^SOURCE_TIME_LIMIT$"):
            adapter.search()
    assert adapter.health() == "SOURCE_TIME_LIMIT"


def test_exhausted_remaining_operation_budget_makes_no_request(payloads):
    class Check:
        def __call__(self):
            pass

        def remaining_seconds(self):
            return 0

    requests = []
    with pytest.raises(SourceError, match="^SOURCE_TIME_LIMIT$"):
        adapter_for(payloads, requests, check=Check()).search()
    assert requests == []


@pytest.mark.parametrize(("error", "code"), (
    (httpx.ReadTimeout("private diagnostic"), "SOURCE_TIMEOUT"),
    (httpx.ConnectError("private diagnostic"), "SOURCE_NETWORK_ERROR"),
))
def test_transport_errors_are_sanitized(payloads, error, code):
    def respond(_request):
        raise error

    adapter = adapter_for(payloads, handler=respond)
    with pytest.raises(SourceError) as failure:
        adapter.search()
    assert str(failure.value) == code and adapter.health() == code


@pytest.mark.parametrize("control_error", (OperationCancelled, OperationTimedOut))
def test_transport_error_does_not_replace_a_local_stop(payloads, control_error):
    stopped = []
    stop = control_error("synthetic control outcome")

    def respond(_request):
        stopped.append(True)
        raise httpx.ReadTimeout("private diagnostic")

    def check():
        if stopped:
            raise stop

    adapter = adapter_for(payloads, handler=respond, check=check)
    with pytest.raises(control_error) as failure:
        adapter.search()
    assert failure.value is stop and adapter.health() == "NOT_CHECKED"


def test_slot_wait_cancellation_does_not_release_an_unowned_slot(payloads):
    calls = []
    stop = OperationCancelled("synthetic slot cancellation")

    def check():
        calls.append(True)
        if len(calls) >= 3:
            raise stop

    adapter = adapter_for(payloads, check=check)
    assert adapter._request_slots.acquire(blocking=False)
    try:
        with pytest.raises(OperationCancelled) as failure:
            adapter.search()
        assert failure.value is stop
        assert not adapter._request_slots.acquire(blocking=False)
    finally:
        adapter._request_slots.release()
    adapter.check = lambda: None
    assert len(adapter.search().items) == 3


def test_real_request_path_reserves_both_starts_on_one_rate_schedule(payloads):
    instant, sleeps, starts = [0.0], [], []

    def sleep(seconds):
        sleeps.append(seconds)
        instant[0] += seconds

    def respond(request):
        starts.append(instant[0])
        return response_for(request, payloads)

    adapter = source.USAJobs(clock=lambda: NOW, rate_limit=600)
    with patch.object(source.time, "monotonic", side_effect=lambda: instant[0]), \
            patch.object(source.time, "sleep", side_effect=sleep), \
            patch.object(source, "SourceTransport", side_effect=lambda _check: httpx.MockTransport(respond)):
        assert len(adapter.search().items) == 3
    assert starts == pytest.approx([0.0, 0.1])
    assert sleeps and all(0 < delay <= source.POLL_INTERVAL for delay in sleeps)


def test_rate_wait_is_cancellable_without_wall_clock_delay():
    stopped = []
    stop = OperationCancelled("synthetic rate cancellation")

    def check():
        if stopped:
            raise stop

    adapter = source.USAJobs(check=check, rate_limit=1)
    with patch.object(source.time, "monotonic", return_value=0):
        adapter._wait_for_rate_limit()
        stopped.append(True)
        with pytest.raises(OperationCancelled) as failure:
            adapter._wait_for_rate_limit()
    assert failure.value is stop


def test_concurrency_limit_bounds_both_searches_requests(payloads):
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    active = maximum = 0
    errors, batches = [], []

    def respond(request):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        entered.set()
        try:
            assert release.wait(2), "Synthetic request was not released"
            return response_for(request, payloads)
        finally:
            with lock:
                active -= 1

    adapter = adapter_for(payloads, handler=respond, concurrency=1)

    def run(second=False):
        if second:
            second_started.set()
        try:
            batches.append(adapter.search())
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run, args=(True,))
    first.start()
    try:
        assert entered.wait(1)
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
    assert errors == [] and len(batches) == 2 and maximum == 1


@pytest.mark.parametrize("label,country,expected", (
    ("Mississippi", "United States", "MS"),
    ("new york", "United States", "NY"),
    ("District of Columbia", "United States", "DC"),
    ("Washington", "United States", "WA"),
    ("ms", "United States", "MS"),
    ("TX", "United States", "TX"),
    ("in transit", "United States", None),
    ("Not New York", "United States", None),
    ("Mississippi", "Canada", None),
    ("CA", "Canada", None),
    ("Texas", "", None),
))
def test_exact_structured_state_names_support_filters_without_country_inference(
        payloads, label, country, expected):
    raw = envelope(payloads)
    raw["metadata"]["positionlocations"] = [{
        "positionlocationcity": "Synthetic City",
        "positionlocationstate": label,
        "positionlocationcountry": country,
    }]
    from services.discovery.search_semantics import evaluate_search

    before = deepcopy(raw)
    job = source.USAJobs().normalize(raw)
    attrs = job["attributes"]
    assert attrs["state"] == expected
    assert attrs["source_locations"][0]["state"] == label
    assert raw == before
    if expected is not None:
        assert evaluate_search({"states": [expected]}, job)["matched"]
    assert "STATES_MISMATCH" in evaluate_search({"states": ["AK"]}, job)["reasons"]
