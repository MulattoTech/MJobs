"""Aggregator labels must not silently reparent jobs through lossy company IDs."""
import unicodedata
from copy import deepcopy
from xml.etree import ElementTree

import httpx
import pytest
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.errors import SourceError
from tests.integration.test_greenhouse import RAW as GREENHOUSE_JOB
from tests.integration.test_remoteok import RAW_REMOTEOK_PAYLOAD
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD

CONFLICT = "SOURCE_COMPANY_IDENTITY_CONFLICT"
PROVIDERS = ("wwr", "remoteok", "remotive")
DOMAIN_TABLES = (
    m.companies, m.company_sources, m.jobs, m.routes, m.postings,
    m.posting_snapshots, m.posting_value_candidates,
)


@pytest.fixture
def store(tmp_path):
    database = Store("sqlite:///" + (tmp_path / "company-identity.db").as_posix())
    database.migrate()
    try:
        yield database
    finally:
        database.engine.dispose()


def response_for(provider, names, *, first_id=8100):
    """Use actual adapters with entirely synthetic, unique native job identities."""
    if provider == "wwr":
        root = ElementTree.Element("rss", version="2.0")
        channel = ElementTree.SubElement(root, "channel")
        for offset, name in enumerate(names):
            item = ElementTree.SubElement(channel, "item")
            identifier = str(first_id + offset)
            fields = {
                "title": name + ": Synthetic Software Engineer",
                "description": "Build synthetic Python services.",
                "region": "Anywhere in the World",
                "guid": "https://weworkremotely.com/remote-jobs/synthetic-" + identifier,
                "link": "https://weworkremotely.com/remote-jobs/synthetic-" + identifier,
            }
            for key, value in fields.items():
                ElementTree.SubElement(item, key).text = value
        return httpx.Response(
            200, content=ElementTree.tostring(root, encoding="utf-8"),
            headers={"content-type": "application/rss+xml"},
        )
    if provider == "remoteok":
        rows = []
        for offset, name in enumerate(names):
            row = deepcopy(RAW_REMOTEOK_PAYLOAD[1])
            identifier = str(first_id + offset)
            row.update(
                id=identifier, company=name,
                url="https://remoteok.com/remote-jobs/synthetic-" + identifier,
                apply_url="https://remoteok.com/remote-jobs/synthetic-" + identifier,
            )
            rows.append(row)
        return httpx.Response(200, json=rows)
    assert provider == "remotive"
    rows = []
    for offset, name in enumerate(names):
        row = deepcopy(RAW_REMOTIVE_PAYLOAD["jobs"][0])
        identifier = first_id + offset
        row.update(
            id=identifier, company_name=name,
            url="https://remotive.com/remote-jobs/software-dev/synthetic-" + str(identifier),
        )
        rows.append(row)
    return httpx.Response(200, json={"jobs": rows, "job-count": len(rows)})


def collect_names(store, provider, names, *, source_id=None, first_id=8100, tenant="default"):
    source_id = source_id or configure_source(
        store, tenant, provider=provider, rate_limit=600, jitter_seconds=0,
    )

    def respond(request):
        assert request.method == "GET"
        return response_for(provider, names, first_id=first_id)

    result = collect_source(
        store, source_id, None, refresh_matches=False,
        transport=httpx.MockTransport(respond),
    )
    return source_id, result


def release_cooldown(store, source_id):
    with store.engine.begin() as connection:
        connection.execute(update(m.job_sources).where(
            m.job_sources.c.id == source_id,
        ).values(last_attempt=None, backoff_until=None))


def domain_snapshot(store):
    with store.engine.connect() as connection:
        return {
            table.name: [dict(row) for row in connection.execute(
                select(table).order_by(table.c.id),
            ).mappings()]
            for table in DOMAIN_TABLES
        }


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("names", (
    ("Synthetic A+B", "Synthetic A B"),
    ("公司甲", "公司乙"),
    ("A" * 85 + "First", "A" * 85 + "Second"),
), ids=("punctuation", "non_ascii", "truncated"))
@pytest.mark.parametrize("reverse", (False, True), ids=("forward", "reverse"))
def test_conflicting_batch_rolls_back_without_choosing_a_company(store, provider, names, reverse):
    source_id = configure_source(store, "default", provider=provider, rate_limit=600)
    before = domain_snapshot(store)
    labels = list(reversed(names)) if reverse else list(names)
    with pytest.raises(SourceError, match="^" + CONFLICT + "$"):
        collect_names(store, provider, ["Unrelated Synthetic Employer", *labels], source_id=source_id)
    assert domain_snapshot(store) == before
    with store.engine.connect() as connection:
        source = connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == source_id,
        )).mappings().one()
        observation = connection.execute(select(m.source_quality_observations)).mappings().one()
    assert source["last_error"] == CONFLICT
    assert source["health_status"] == "FAILED"
    assert source["consecutive_failures"] == 1
    assert observation["error_code"] == CONFLICT
    assert observation["succeeded"] is False
    assert observation["available"] is True


@pytest.mark.parametrize("provider", PROVIDERS)
def test_reopened_collection_rejects_conflicting_stored_label_without_partial_writes(store, provider):
    source_id, result = collect_names(store, provider, ["Synthetic A+B"])
    assert result["jobs_seen"] == 1
    before = domain_snapshot(store)
    database_url = store.engine.url.render_as_string(hide_password=False)
    store.engine.dispose()
    reopened = Store(database_url)
    try:
        release_cooldown(reopened, source_id)
        with pytest.raises(SourceError, match="^" + CONFLICT + "$"):
            collect_names(
                reopened, provider, ["Unrelated Synthetic Employer", "Synthetic A B"],
                source_id=source_id, first_id=9200,
            )
        assert domain_snapshot(reopened) == before
    finally:
        reopened.engine.dispose()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_equivalent_label_formatting_retains_idempotent_jobs_and_original_name(store, provider):
    source_id, result = collect_names(
        store, provider, ["Synthetic Employer", "Synthetic Employer"],
    )
    assert result["jobs_seen"] == 2
    release_cooldown(store, source_id)
    collect_names(
        store, provider, ["SYNTHETIC   EMPLOYER", " synthetic employer "],
        source_id=source_id,
    )
    with store.engine.connect() as connection:
        company = connection.execute(select(m.companies)).mappings().one()
        jobs = connection.execute(select(m.jobs)).mappings().all()
        postings = connection.execute(select(m.postings)).mappings().all()
    assert company["name"] == "Synthetic Employer"
    assert len(jobs) == len(postings) == 2
    assert {job["company_id"] for job in jobs} == {company["id"]}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_canonical_unicode_labels_are_compatible(store, provider):
    name = "Εταιρεία"
    source_id, _ = collect_names(store, provider, [name])
    release_cooldown(store, source_id)
    collect_names(
        store, provider, [unicodedata.normalize("NFD", name)],
        source_id=source_id,
    )
    with store.engine.connect() as connection:
        assert connection.execute(select(m.companies.c.name)).scalar_one() == name
        assert len(connection.execute(select(m.jobs.c.id)).all()) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_noncolliding_employers_remain_separate(store, provider):
    _, result = collect_names(store, provider, ["Synthetic Employer A", "Synthetic Employer B"])
    assert result["jobs_seen"] == 2
    with store.engine.connect() as connection:
        companies = connection.execute(select(m.companies)).mappings().all()
        jobs = connection.execute(select(m.jobs)).mappings().all()
    assert len(companies) == len(jobs) == 2
    assert {company["id"] for company in companies} == {job["company_id"] for job in jobs}


def test_first_party_ats_name_updates_remain_supported(store):
    source_id = configure_source(store, "example", provider="greenhouse", rate_limit=600)
    current_name = ["Original Synthetic Employer"]

    def respond(request):
        assert request.method == "GET"
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [GREENHOUSE_JOB], "meta": {"total": 1}})
        return httpx.Response(200, json={"name": current_name[0]})

    transport = httpx.MockTransport(respond)
    collect_source(store, source_id, None, transport=transport, refresh_matches=False)
    release_cooldown(store, source_id)
    current_name[0] = "Renamed Synthetic Employer"
    collect_source(store, source_id, None, transport=transport, refresh_matches=False)
    with store.engine.connect() as connection:
        assert connection.execute(select(m.companies.c.name)).scalar_one() == current_name[0]
        assert len(connection.execute(select(m.jobs.c.id)).all()) == 1
