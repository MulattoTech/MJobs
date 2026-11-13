"""Synthetic Arbeitnow collection proves persistence and source-authority boundaries."""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.errors import SourceError

API_URL = "https://www.arbeitnow.com/api/job-board-api"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "arbeitnow.json"
DOMAIN_TABLES = (
    m.companies, m.company_sources, m.jobs, m.routes, m.postings,
    m.posting_snapshots, m.posting_value_candidates, m.job_lifecycle_observations,
)


@pytest.fixture
def store(tmp_path):
    database = Store("sqlite:///" + (tmp_path / "arbeitnow.db").as_posix())
    database.migrate()
    try:
        yield database
    finally:
        database.engine.dispose()


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def source(store, **options):
    return configure_source(store, "default", provider="arbeitnow",
                            rate_limit=600, jitter_seconds=0, **options)


def collect(store, source_id, body, requests=None):
    def respond(request):
        assert request.method == "GET"
        assert str(request.url) == API_URL
        if requests is not None:
            requests.append(str(request.url))
        return httpx.Response(200, json=body)
    return collect_source(store, source_id, None, refresh_matches=False,
                          transport=httpx.MockTransport(respond))


def rows(store, table):
    with store.engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(table).order_by(table.c.id)).mappings()]


def source_row(store, source_id):
    with store.engine.connect() as connection:
        return dict(connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == source_id)).mappings().one())


def make_poll_due(store, source_id):
    # Move only the owned synthetic fixture's admission metadata. No live
    # provider retry or production clock is changed by these contract tests.
    with store.engine.begin() as connection:
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
            last_attempt="2000-01-01T00:00:00+00:00", backoff_until=None))


def domain_snapshot(store):
    return {table.name: rows(store, table) for table in DOMAIN_TABLES}


def test_configured_feed_reopens_with_raw_provenance_and_manual_routes(store):
    body = payload()
    # Untrusted extra fields must remain raw evidence, never employer authority.
    body["data"][0]["company_url"] = "https://employer.invalid"
    body["data"][0]["apply_url"] = "https://boards.greenhouse.io/unverified/jobs/1"
    requests = []
    source_id = source(store)
    result = collect(store, source_id, body, requests)
    assert result["jobs_seen"] == 2
    assert result["rejected"] == 0
    assert result["strategy"] == "PUBLIC_JSON"
    assert result["direct_application"] is False
    assert requests == [API_URL]  # The response's next-page link is not followed.

    url = str(store.engine.url)
    store.engine.dispose()
    reopened = Store(url)
    try:
        companies = rows(reopened, m.companies)
        jobs = rows(reopened, m.jobs)
        postings = rows(reopened, m.postings)
        snapshots = rows(reopened, m.posting_snapshots)
        company_sources = rows(reopened, m.company_sources)
        assert len(companies) == len(jobs) == len(postings) == len(snapshots) == 2
        assert {company["name"] for company in companies} == {
            item["company_name"] for item in body["data"]}
        assert all(company["domain"] == "" for company in companies)
        assert rows(reopened, m.company_ats_tenants) == []
        assert rows(reopened, m.company_domains) == []
        assert rows(reopened, m.applications) == []
        assert all(item["ats_tenant_id"] is None for item in company_sources)
        assert all(item["collector_source_id"] == source_id for item in company_sources)
        by_slug = {item["slug"]: item for item in body["data"]}
        assert {row["raw"]["slug"]: row["raw"] for row in postings} == by_slug
        assert {row["raw"]["slug"]: row["raw"] for row in snapshots} == by_slug
        assert {route["url"] for route in rows(reopened, m.routes)} == {
            item["url"] for item in body["data"]}
        for job in jobs:
            raw = by_slug[job["requisition"]]
            attrs = job["attributes"]
            assert attrs["provider"] == "arbeitnow"
            assert attrs["source_endpoint"] == API_URL
            assert attrs["provider_listing_url"] == raw["url"]
            assert attrs["submission_capability"] == "MANUAL_ONLY"
            assert attrs["source_resolution"]["authority"] == "AGGREGATOR"
            assert attrs["salary_min"] is None and attrs["salary_max"] is None
            assert attrs["salary_source"] == "UNKNOWN"
            assert attrs["source_attribution"] == {"name": "Arbeitnow", "url": "https://www.arbeitnow.com"}
        remote = next(job for job in jobs if job["requisition"] == body["data"][0]["slug"])
        assert remote["remote_type"] == "REMOTE"
        assert remote["attributes"]["country"] != "US"
        assert "WORLDWIDE" not in remote["attributes"]["remote_regions"]
        assert source_row(reopened, source_id)["health_status"] == "HEALTHY"
    finally:
        reopened.engine.dispose()


def test_identical_duplicate_and_repeated_poll_keep_job_posting_snapshot_identity(store):
    body = payload()
    body["data"].append(deepcopy(body["data"][0]))
    source_id = source(store)
    collect(store, source_id, body)
    before = {table.name: {row["id"] for row in rows(store, table)}
              for table in (m.companies, m.jobs, m.routes, m.postings, m.posting_snapshots)}
    assert all(len(values) == 2 for values in before.values())
    original_raw = {row["id"]: row["raw"] for row in rows(store, m.posting_snapshots)}
    make_poll_due(store, source_id)
    collect(store, source_id, body)
    assert {table.name: {row["id"] for row in rows(store, table)}
            for table in (m.companies, m.jobs, m.routes, m.postings, m.posting_snapshots)} == before
    assert {row["id"]: row["raw"] for row in rows(store, m.posting_snapshots)} == original_raw


def test_conflicting_duplicate_rolls_back_existing_and_new_domain_rows(store):
    body = payload()
    source_id = source(store)
    collect(store, source_id, {"data": [body["data"][0]]})
    before = domain_snapshot(store)
    conflict = deepcopy(body["data"][0])
    conflict["title"] = "Conflicting title for the same native posting"
    body["data"].append(conflict)
    make_poll_due(store, source_id)
    with pytest.raises(SourceError, match="^SOURCE_DUPLICATE_JOB_ID$"):
        collect(store, source_id, body)
    assert domain_snapshot(store) == before
    state = source_row(store, source_id)
    assert state["health_status"] == "FAILED"
    assert state["last_error"] == "SOURCE_DUPLICATE_JOB_ID"


def test_provider_company_slug_cannot_silently_relabel_existing_company(store):
    body = payload()
    source_id = source(store)
    collect(store, source_id, {"data": [body["data"][0]]})
    before = domain_snapshot(store)
    body["data"][0]["company_name"] = "Different Synthetic Employer"
    make_poll_due(store, source_id)
    with pytest.raises(SourceError, match="^SOURCE_COMPANY_IDENTITY_CONFLICT$"):
        collect(store, source_id, body)
    assert domain_snapshot(store) == before
    assert source_row(store, source_id)["last_error"] == "SOURCE_COMPANY_IDENTITY_CONFLICT"


def test_same_native_slug_on_distinct_provider_regions_keeps_separate_identity(store):
    first = payload()["data"][0]
    regional = deepcopy(first)
    regional["url"] = first["url"].replace("www.arbeitnow.com", "www.arbeitnow.co.uk")
    source_id = source(store)
    collect(store, source_id, {"data": [first, regional]})
    companies, jobs, postings = (rows(store, table) for table in (m.companies, m.jobs, m.postings))
    assert len(companies) == len(jobs) == len(postings) == 2
    assert len({job["company_id"] for job in jobs}) == 2
    assert {posting["application_url"] for posting in postings} == {first["url"], regional["url"]}


def test_latest_page_omission_and_empty_page_never_close_previously_observed_job(store):
    body = payload()
    source_id = source(store)
    collect(store, source_id, body)
    original_ids = {row["id"] for row in rows(store, m.jobs)}
    for latest in ({"data": [body["data"][0]]}, {"data": []}):
        make_poll_due(store, source_id)
        collect(store, source_id, latest)
    assert {row["id"] for row in rows(store, m.jobs)} == original_ids
    observations = rows(store, m.job_lifecycle_observations)
    assert observations
    assert {row["status"] for row in observations} == {"POSSIBLY_ACTIVE"}
    assert not any(row["evidence"].get("kind") == "ABSENT_FROM_COMPLETE_INVENTORY"
                   for row in observations)


def test_disabled_source_and_provider_backoff_prevent_requests_then_recover(store):
    source_id = source(store, enabled=False)
    requests = []
    with pytest.raises(SourceError, match="^SOURCE_DISABLED$"):
        collect(store, source_id, payload(), requests)
    assert requests == []
    assert source_row(store, source_id)["last_attempt"] is None
    assert source(store, enabled=True) == source_id

    def rate_limited(request):
        requests.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "120"},
                              text="SYNTHETIC_PRIVATE_PROVIDER_ERROR")
    with pytest.raises(SourceError, match="^SOURCE_RATE_LIMITED$"):
        collect_source(store, source_id, None, refresh_matches=False,
                       transport=httpx.MockTransport(rate_limited))
    state = source_row(store, source_id)
    assert state["health_status"] == "RATE_LIMITED"
    assert state["consecutive_failures"] == 1
    assert state["last_error"] == "SOURCE_RATE_LIMITED"
    assert state["backoff_until"] > state["last_attempt"]
    with pytest.raises(SourceError, match="^SOURCE_BACKOFF$"):
        collect(store, source_id, payload(), requests)
    assert requests == [API_URL]
    assert not rows(store, m.jobs)
    assert "SYNTHETIC_PRIVATE_PROVIDER_ERROR" not in json.dumps(rows(store, m.audit))
    make_poll_due(store, source_id)
    collect(store, source_id, payload(), requests)
    recovered = source_row(store, source_id)
    assert recovered["health_status"] == "HEALTHY"
    assert recovered["consecutive_failures"] == 0
    assert recovered["last_error"] is None and recovered["backoff_until"] is None
    assert requests == [API_URL, API_URL]


def test_invalid_optional_job_fields_report_partial_then_recover_without_data_loss(store):
    body = payload()
    malformed = deepcopy(body)
    malformed["data"][1]["title"] = None
    source_id = source(store)
    result = collect(store, source_id, malformed)
    assert result["jobs_seen"] == 1 and result["rejected"] == 1
    assert source_row(store, source_id)["health_status"] == "PARTIAL"
    retained = rows(store, m.jobs)[0]["id"]
    make_poll_due(store, source_id)
    collect(store, source_id, body)
    assert len(rows(store, m.jobs)) == 2
    assert retained in {row["id"] for row in rows(store, m.jobs)}
    assert source_row(store, source_id)["health_status"] == "HEALTHY"


def test_public_api_enrolls_registered_provider_and_rejects_invalid_tenant(store, tmp_path):
    from fastapi.testclient import TestClient
    from apps.api.main import create_app

    with TestClient(create_app(store, "http://127.0.0.1:1", tmp_path,
                              "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
        invalid = client.put("/sources", json={"provider": "arbeitnow", "tenant": "arbitrary"})
        assert invalid.status_code == 422
        assert rows(store, m.job_sources) == []
        created = client.put("/sources", json={
            "provider": "arbeitnow", "tenant": "default", "poll_interval": 3600,
        })
        assert created.status_code == 200
        configured = client.get("/sources").json()["sources"]
        assert len(configured) == 1
        current = configured[0]
        assert current["id"] == created.json()["id"]
        assert (current["provider"], current["tenant"]) == ("arbeitnow", "default")
        assert current["strategy"] == "PUBLIC_JSON"
        assert set(current["capabilities"]["capabilities"]) == {
            "DISCOVERY", "REMOTE_METADATA", "APPLICATION_REDIRECT",
        }
        assert not current["capabilities"]["direct_application"]
        disabled = client.put("/sources", json={
            "provider": "arbeitnow", "tenant": "default", "enabled": False,
        })
        assert disabled.status_code == 200
        assert disabled.json()["id"] == current["id"]
        assert client.get("/sources").json()["sources"][0]["enabled"] is False
        assert rows(store, m.worker_operations) == []
        assert rows(store, m.jobs) == []


@pytest.mark.parametrize("domain", [
    "www.arbeitnow.com", "www.arbeitnow.co.uk", "www.arbeitnow.fr",
    "www.arbeitnow.ch", "www.arbeitnow.nl",
])
def test_provider_domains_cannot_be_promoted_to_verified_employer(domain):
    from services.company_discovery.domains import resolve_official_domain

    result = resolve_official_domain([{
        "domain": domain,
        "method": "FIRST_PARTY_JOB_LINK",
        "source_type": "EMPLOYER",
        "evidence": ["An untrusted posting labels its aggregator as an employer"],
    }])
    assert result["status"] == "NOT_FOUND"
    assert result["domain"] == ""
