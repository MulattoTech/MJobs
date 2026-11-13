"""Synthetic USAJOBS collection exercises durable joins and manual-only authority."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.sources import usajobs
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.errors import SourceError

METADATA_URL = "https://data.usajobs.gov/api/historicjoa"
TEXT_URL = "https://data.usajobs.gov/api/historicjoa/announcementtext"
FIXTURES = Path(__file__).parents[1] / "fixtures"
DOMAIN_TABLES = (
    m.companies, m.company_sources, m.jobs, m.routes, m.postings,
    m.posting_snapshots, m.posting_value_candidates, m.job_lifecycle_observations,
)


class FixtureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 11, 13, 12, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fixture_calendar(monkeypatch):
    # Freeze only the provider calendar. Transport deadlines keep real clocks.
    monkeypatch.setattr(usajobs, "datetime", FixtureDateTime)


@pytest.fixture
def store(tmp_path):
    database = Store("sqlite:///" + (tmp_path / "usajobs.db").as_posix())
    database.migrate()
    try:
        yield database
    finally:
        database.engine.dispose()


def feed(*controls):
    bodies = [json.loads((FIXTURES / name).read_text(encoding="utf-8")) for name in (
        "usajobs_metadata.json", "usajobs_announcement_text.json")]
    if controls:
        for body in bodies:
            body["data"] = [row for row in body["data"]
                            if row["usajobsControlNumber"] in controls]
    return bodies


def source(store, **options):
    return configure_source(store, "default", provider="usajobs",
                            rate_limit=600, jitter_seconds=0, **options)


def transport(metadata, announcement_text, requests=None, *, limited_endpoint=None):
    def respond(request):
        endpoint = str(request.url).partition("?")[0]
        assert request.method == "GET"
        assert endpoint in {METADATA_URL, TEXT_URL}
        assert not ({"authorization", "x-api-key", "api-key"} & set(request.headers))
        parameters = dict(request.url.params)
        if requests is not None:
            requests.append((endpoint, parameters))
        assert parameters == {
            "PositionSeries": "2210",
            "StartPositionOpenDate": "11-07-2026",
            "EndPositionOpenDate": "11-13-2026",
        }
        body = metadata if endpoint == METADATA_URL else announcement_text
        if endpoint == limited_endpoint:
            return httpx.Response(429, headers={"Retry-After": "120"},
                                  text="Synthetic upstream private failure body")
        return httpx.Response(200, json=body)
    return httpx.MockTransport(respond)


def collect(store, source_id, bodies, requests=None, **options):
    return collect_source(store, source_id, None, refresh_matches=False,
                          transport=transport(*bodies, requests, **options))


def rows(store, table):
    with store.engine.connect() as connection:
        return [dict(row) for row in connection.execute(
            select(table).order_by(table.c.id)).mappings()]


def source_row(store, source_id):
    with store.engine.connect() as connection:
        return dict(connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == source_id)).mappings().one())


def make_poll_due(store, source_id):
    # Change only admission metadata in this test's isolated SQLite store.
    with store.engine.begin() as connection:
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
            last_attempt="2000-01-01T00:00:00+00:00", backoff_until=None))


def domain_snapshot(store):
    return {table.name: rows(store, table) for table in DOMAIN_TABLES}


def test_joined_source_reopens_with_raw_snapshots_native_agencies_and_manual_routes(store):
    bodies = feed()
    bodies[0]["data"][0]["company_url"] = "https://employer.invalid"
    bodies[1]["data"][0]["apply_url"] = "https://boards.greenhouse.io/unverified/jobs/1"
    originals = deepcopy(bodies)
    calls = []
    source_id = source(store)
    result = collect(store, source_id, bodies, calls)
    assert result["jobs_seen"] == 3 and result["rejected"] == 0
    assert result["strategy"] == "PUBLIC_JSON"
    assert result["direct_application"] is False
    assert bodies == originals
    assert [endpoint for endpoint, _ in calls] == [METADATA_URL, TEXT_URL]
    assert calls[1][1] == calls[0][1]

    texts = {row["usajobsControlNumber"]: row for row in bodies[1]["data"]}
    expected = {str(row["usajobsControlNumber"]): {
        "metadata": row, "announcement_text": texts[row["usajobsControlNumber"]],
    } for row in bodies[0]["data"]}
    url = str(store.engine.url)
    store.engine.dispose()
    reopened = Store(url)
    try:
        companies = rows(reopened, m.companies)
        assert {row["id"] for row in companies} == {
            identity("usajobs", "ZZ", "ZZIT"), identity("usajobs", "ZZ", "ZZIV")}
        assert {row["name"] for row in companies} == {"USAJOBS agency ZZIT", "USAJOBS agency ZZIV"}
        assert all(row["domain"] == "" for row in companies)
        company_sources = rows(reopened, m.company_sources)
        assert len(company_sources) == 2
        assert all(row["ats_tenant_id"] is None for row in company_sources)
        assert all(row["collector_source_id"] == source_id for row in company_sources)
        for table in (m.company_ats_tenants, m.company_domains, m.applications, m.operations):
            assert rows(reopened, table) == []
        for table in (m.postings, m.posting_snapshots):
            stored = rows(reopened, table)
            assert len(stored) == 3
            assert {str(row["raw"]["metadata"]["usajobsControlNumber"]): row["raw"]
                    for row in stored} == expected
        routes = rows(reopened, m.routes)
        assert len(routes) == 3
        assert {row["url"] for row in routes} == {
            "https://www.usajobs.gov/job/" + control for control in expected}
        assert all(row["provider"] == "usajobs" and row["tenant"] == "default" for row in routes)
        jobs = rows(reopened, m.jobs)
        assert len(jobs) == 3
        for job in jobs:
            native = expected[job["requisition"]]["metadata"]
            attrs = job["attributes"]
            assert job["company_id"] == identity(
                "usajobs", native["hiringDepartmentCode"], native["hiringAgencyCode"])
            assert attrs["provider"] == "usajobs"
            assert attrs["source_endpoint"] == METADATA_URL
            assert attrs["source_endpoints"] == [METADATA_URL, TEXT_URL]
            assert attrs["submission_capability"] == "MANUAL_ONLY"
            assert attrs["source_resolution"]["authority"] == "AGGREGATOR"
            assert attrs["application_url_status"] == "CONSTRUCTED_UNVERIFIED"
            assert attrs["route_confirmation"] == "NOT_RUN"
            assert attrs["provider_listing_url"] == "https://www.usajobs.gov/job/" + job["requisition"]
            assert attrs["source_attribution"] == {
                "name": "USAJOBS", "url": "https://www.usajobs.gov", "unofficial": True}
            assert attrs["source_open_date"] == native["positionOpenDate"]
            assert attrs["source_close_date"] == native["positionCloseDate"]
            assert attrs["source_expire_date"] == native["positionExpireDate"]
            assert attrs["source_open_date_precision"] == "DATE"
            assert attrs["source_date_timezone"] is None
            assert attrs["reported_posted_at"] is None
            assert attrs["reported_posted_at_raw"] is None
            assert job["remote_type"] == "UNKNOWN" and attrs["remote_regions"] == []
            assert attrs["salary_min"] is None and attrs["salary_max"] is None
            assert attrs["salary_source"] == "UNKNOWN"
            assert attrs["source_salary_claim"] == {key: native[key] for key in (
                "minimumSalary", "maximumSalary", "salaryType")}
            assert "<script>" not in job["description"]
            assert "private_script_value" not in job["description"]
        foreign = next(job for job in jobs if job["requisition"] == "990000103")
        assert foreign["attributes"]["country"] != "US"
        assert source_row(reopened, source_id)["health_status"] == "HEALTHY"
    finally:
        reopened.engine.dispose()


def test_identical_duplicates_and_repeat_keep_canonical_and_raw_snapshot_identity(store):
    bodies = feed()
    for body in bodies:
        body["data"].append(deepcopy(body["data"][0]))
    source_id = source(store)
    assert collect(store, source_id, bodies)["jobs_seen"] == 3
    tables = (m.companies, m.jobs, m.routes, m.postings, m.posting_snapshots)
    before = {table.name: {row["id"] for row in rows(store, table)} for table in tables}
    raw_before = {row["id"]: row["raw"] for row in rows(store, m.posting_snapshots)}
    make_poll_due(store, source_id)
    assert collect(store, source_id, bodies)["jobs_seen"] == 3
    assert {table.name: {row["id"] for row in rows(store, table)} for table in tables} == before
    assert {row["id"]: row["raw"] for row in rows(store, m.posting_snapshots)} == raw_before


@pytest.mark.parametrize("unsupported_identity", (False, True))
def test_unselected_bulk_text_creates_no_company_or_posting_records(store, unsupported_identity):
    bodies = feed(990000101)
    extra = deepcopy(bodies[1]["data"][0])
    extra.update(usajobsControlNumber=990099999, hiringAgencyCode="ZZEX",
                 hiringDepartmentCode="YY", announcementNumber="SYN-UNSELECTED")
    if unsupported_identity:
        extra.update(hiringAgencyCode="Other Agencies and Independent Organizations",
                     hiringDepartmentCode=None, announcementNumber=None)
    bodies[1]["data"].insert(0, extra)
    original = deepcopy(bodies)
    calls = []
    result = collect(store, source(store), bodies, calls)
    assert result["jobs_seen"] == 1 and result["rejected"] == 0
    assert len(calls) == 2 and calls[0][1] == calls[1][1]
    assert bodies == original
    assert {row["id"] for row in rows(store, m.companies)} == {
        identity("usajobs", "ZZ", "ZZIT")}
    assert len(rows(store, m.company_sources)) == 1
    assert {row["requisition"] for row in rows(store, m.jobs)} == {"990000101"}
    assert len(rows(store, m.routes)) == 1
    for table in (m.postings, m.posting_snapshots):
        stored = rows(store, table)
        assert len(stored) == 1
        assert stored[0]["raw"] == {
            "metadata": bodies[0]["data"][0],
            "announcement_text": bodies[1]["data"][1],
        }
    for table in (m.company_ats_tenants, m.company_domains, m.applications, m.operations):
        assert rows(store, table) == []


def test_agency_display_name_changes_preserve_identity_and_append_raw_history(store):
    bodies = feed(990000101)
    source_id = source(store)
    collect(store, source_id, bodies)
    company = rows(store, m.companies)[0]
    job = rows(store, m.jobs)[0]
    old_snapshot = rows(store, m.posting_snapshots)[0]
    bodies[0]["data"][0]["hiringAgencyName"] = None
    bodies[1]["data"][0]["duties"] = "A revised synthetic duty: maintain the Linux backup service."
    make_poll_due(store, source_id)
    collect(store, source_id, bodies)
    assert rows(store, m.companies) == [company]
    current = rows(store, m.jobs)[0]
    assert current["id"] == job["id"] and current["company_id"] == company["id"]
    assert "Linux backup service" in current["description"]
    assert current["attributes"]["source_agency_name"] is None
    snapshots = rows(store, m.posting_snapshots)
    assert len(snapshots) == 2
    assert next(row for row in snapshots if row["id"] == old_snapshot["id"]) == old_snapshot
    assert len(rows(store, m.postings)) == len(rows(store, m.routes)) == 1


def test_same_agency_code_in_different_departments_stays_separate(store):
    bodies = feed(990000101)
    for body in bodies:
        other = deepcopy(body["data"][0])
        other["usajobsControlNumber"] = 990000104
        other["announcementNumber"] = "SYN-IT-2026-104"
        other["hiringDepartmentCode"] = "YY"
        body["data"].append(other)
    collect(store, source(store), bodies)
    assert {row["id"] for row in rows(store, m.companies)} == {
        identity("usajobs", "ZZ", "ZZIT"), identity("usajobs", "YY", "ZZIT")}
    assert len({row["company_id"] for row in rows(store, m.jobs)}) == 2
    assert len(rows(store, m.postings)) == 2


@pytest.mark.parametrize("defect,code", [
    ("missing_text", "SOURCE_JOIN_MISSING"),
    ("missing_selected_with_extras", "SOURCE_JOIN_MISSING"),
    ("mismatched_agency", "SOURCE_JOIN_MISMATCH"),
    ("conflicting_metadata", "SOURCE_JOIN_CONFLICT"),
    ("conflicting_text", "SOURCE_JOIN_CONFLICT"),
    ("conflicting_unselected_text", "SOURCE_JOIN_CONFLICT"),
])
def test_enrichment_failure_preserves_all_existing_domain_rows(store, defect, code):
    source_id = source(store)
    collect(store, source_id, feed(990000101))
    before = domain_snapshot(store)
    bodies = feed()
    if defect == "missing_text":
        bodies[1]["data"].pop()
    elif defect == "missing_selected_with_extras":
        bodies[1]["data"][-1]["usajobsControlNumber"] = 990099999
    elif defect == "conflicting_unselected_text":
        extra = deepcopy(bodies[1]["data"][0])
        extra.update(usajobsControlNumber=990099999,
                     hiringAgencyCode="Other Agencies and Independent Organizations")
        conflict = deepcopy(extra)
        conflict["summary"] = "Conflicting unselected bulk announcement"
        bodies[1]["data"].extend((extra, conflict))
    elif defect == "mismatched_agency":
        bodies[1]["data"][1]["hiringAgencyCode"] = "OTHER"
    elif defect == "conflicting_metadata":
        conflict = deepcopy(bodies[0]["data"][0])
        conflict["positionTitle"] = "Conflicting title for one native control number"
        bodies[0]["data"].append(conflict)
    else:
        conflict = deepcopy(bodies[1]["data"][0])
        conflict["summary"] = "Conflicting announcement text for one native control number"
        bodies[1]["data"].append(conflict)
    make_poll_due(store, source_id)
    with pytest.raises(SourceError, match="^" + code + "$"):
        collect(store, source_id, bodies)
    assert domain_snapshot(store) == before
    state = source_row(store, source_id)
    assert state["health_status"] == "FAILED" and state["last_error"] == code


def test_explicit_status_is_preserved_but_dates_and_rolling_omission_never_close_jobs(store):
    bodies = feed()
    for body in bodies:
        row = next(item for item in body["data"] if item["usajobsControlNumber"] == 990000103)
        row["positionCloseDate"] = "2000-01-01"
        row["positionExpireDate"] = "2000-01-02"
    source_id = source(store)
    collect(store, source_id, bodies)
    statuses = {job["requisition"]: job["attributes"]["status"] for job in rows(store, m.jobs)}
    assert statuses == {"990000101": "POSSIBLY_ACTIVE", "990000102": "CLOSED", "990000103": "UNKNOWN"}
    closed = next(job for job in rows(store, m.jobs) if job["requisition"] == "990000102")
    assert closed["attributes"]["source_opening_status"] == "Applications under review"
    assert closed["attributes"]["lifecycle_claim"] == "CLOSED"
    before_ids = {job["id"] for job in rows(store, m.jobs)}
    make_poll_due(store, source_id)
    collect(store, source_id, feed(990000101))
    before_empty = rows(store, m.job_lifecycle_observations)
    calls = []
    make_poll_due(store, source_id)
    assert collect(store, source_id, [{"data": []}, {"data": []}], calls)["jobs_seen"] == 0
    assert [endpoint for endpoint, _ in calls] == [METADATA_URL]
    assert rows(store, m.job_lifecycle_observations) == before_empty
    assert {job["id"] for job in rows(store, m.jobs)} == before_ids
    assert {job["requisition"]: job["attributes"]["status"] for job in rows(store, m.jobs)} == statuses
    assert not any(row["evidence"].get("kind") == "ABSENT_FROM_COMPLETE_INVENTORY"
                   for row in before_empty)
    assert not ({"REMOVED", "EXPIRED", "FILLED"} & {row["status"] for row in before_empty})


@pytest.mark.parametrize("limited_endpoint", [METADATA_URL, TEXT_URL])
def test_disabled_source_and_rate_limit_admission_do_not_import_then_recover(store, limited_endpoint):
    source_id = source(store, enabled=False)
    calls = []
    with pytest.raises(SourceError, match="^SOURCE_DISABLED$"):
        collect(store, source_id, feed(), calls)
    assert calls == [] and source_row(store, source_id)["last_attempt"] is None
    assert source(store, enabled=True) == source_id
    with pytest.raises(SourceError, match="^SOURCE_RATE_LIMITED$"):
        collect(store, source_id, feed(), calls, limited_endpoint=limited_endpoint)
    state = source_row(store, source_id)
    assert state["health_status"] == "RATE_LIMITED"
    assert state["consecutive_failures"] == 1 and state["last_error"] == "SOURCE_RATE_LIMITED"
    assert state["backoff_until"] > state["last_attempt"]
    called_before = list(calls)
    with pytest.raises(SourceError, match="^SOURCE_BACKOFF$"):
        collect(store, source_id, feed(), calls)
    assert calls == called_before
    assert all(rows(store, table) == [] for table in DOMAIN_TABLES)
    assert "Synthetic upstream private failure body" not in json.dumps(rows(store, m.audit))
    make_poll_due(store, source_id)
    collect(store, source_id, feed(), calls)
    recovered = source_row(store, source_id)
    assert recovered["health_status"] == "HEALTHY" and recovered["consecutive_failures"] == 0
    assert recovered["last_error"] is None and recovered["backoff_until"] is None
    assert len(rows(store, m.jobs)) == 3


def test_normalization_rejection_reports_partial_and_later_recovers_without_replacing_jobs(store):
    bodies = feed()
    bodies[0]["data"][1]["positionTitle"] = None
    source_id = source(store)
    result = collect(store, source_id, bodies)
    assert result["jobs_seen"] == 2 and result["rejected"] == 1
    assert source_row(store, source_id)["health_status"] == "PARTIAL"
    retained = {row["id"] for row in rows(store, m.jobs)}
    make_poll_due(store, source_id)
    result = collect(store, source_id, feed())
    assert result["jobs_seen"] == 3 and result["rejected"] == 0
    assert retained <= {row["id"] for row in rows(store, m.jobs)}
    assert source_row(store, source_id)["health_status"] == "HEALTHY"


def test_public_api_enrolls_exact_capabilities_without_collecting_or_creating_applications(store, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api.main import create_app

    def forbid_provider_request(*_args, **_kwargs):
        pytest.fail("Source enrollment must not contact USAJOBS")
    monkeypatch.setattr(usajobs.USAJobs, "_request", forbid_provider_request)
    with TestClient(create_app(store, "http://127.0.0.1:1", tmp_path,
                              "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
        invalid = client.put("/sources", json={"provider": "usajobs", "tenant": "arbitrary"})
        assert invalid.status_code == 422 and rows(store, m.job_sources) == []
        created = client.put("/sources", json={
            "provider": "usajobs", "tenant": "default", "poll_interval": 3600,
        })
        assert created.status_code == 200
        configured = client.get("/sources").json()["sources"]
        assert len(configured) == 1
        current = configured[0]
        assert current["id"] == created.json()["id"]
        assert (current["provider"], current["tenant"]) == ("usajobs", "default")
        assert current["strategy"] == "PUBLIC_JSON"
        assert set(current["capabilities"]["capabilities"]) == {"DISCOVERY", "APPLICATION_REDIRECT"}
        assert current["capabilities"]["direct_application"] is False
        disabled = client.put("/sources", json={
            "provider": "usajobs", "tenant": "default", "enabled": False,
        })
        assert disabled.status_code == 200 and disabled.json()["id"] == current["id"]
        assert client.get("/sources").json()["sources"][0]["enabled"] is False
        assert source_row(store, current["id"])["last_attempt"] is None
        for table in (m.operations, m.jobs, m.applications, m.company_ats_tenants):
            assert rows(store, table) == []


@pytest.mark.parametrize("domain", ["usajobs.gov", "www.usajobs.gov", "data.usajobs.gov"])
def test_government_listing_origin_is_not_verified_agency_domain(domain):
    from services.company_discovery.domains import resolve_official_domain

    result = resolve_official_domain([{
        "domain": domain, "method": "FIRST_PARTY_JOB_LINK", "source_type": "EMPLOYER",
        "evidence": ["Untrusted job text claims the listing service is an employer"],
    }])
    assert result["status"] == "NOT_FOUND" and result["domain"] == ""
