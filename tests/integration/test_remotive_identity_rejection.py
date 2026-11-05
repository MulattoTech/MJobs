"""Malformed Remotive identities must not discard valid sibling postings."""
from copy import deepcopy
import sys

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.errors import SourceError
from services.discovery.sources.remotive import Remotive
from tests.integration.test_spine import PROFILE


FIRST = {
    "id": 1001,
    "title": "Synthetic Python Engineer",
    "company_name": "Example Engineering",
    "url": "https://remotive.com/remote-jobs/software-dev/example-1001",
    "description": "<p>Maintain Python services.</p>",
    "publication_date": "2026-10-01T12:00:00",
    "candidate_required_location": "USA Only",
}
SECOND = dict(FIRST, id=1002, title="Synthetic Support Engineer",
              url="https://remotive.com/remote-jobs/devops/example-1002")


@pytest.fixture
def integer_limit():
    """Use CPython's supported minimum limit and restore the process setting."""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.parametrize("native_id", ["\u00b2", "\u2460", "9" * 641, 10 ** 640],
                         ids=["superscript", "circled-digit", "long-string", "large-int"])
def test_unconvertible_native_id_is_a_record_rejection(native_id, integer_limit):
    raw = dict(FIRST, id=native_id)
    with pytest.raises(SourceError) as caught:
        Remotive().normalize(raw)
    assert caught.value.code == "SOURCE_INVALID_JOB"
    assert str(caught.value) == "SOURCE_INVALID_JOB"
    assert caught.value.__cause__ is None
    assert raw["id"] == native_id


@pytest.mark.parametrize("native_id", [None, True, False, 1.5, [], {}, "", "   "],
                         ids=["null", "true", "false", "float", "list", "dict", "empty", "spaces"])
def test_existing_invalid_id_rejections_remain(native_id):
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        Remotive().normalize(dict(FIRST, id=native_id))


@pytest.mark.parametrize(("native_id", "expected"), [
    (1001, 1001), ("1001", 1001), ("001001", 1001),
    ("\u0661\u0660\u0660\u0661", 1001), ("job-1001", "job-1001"),
])
def test_supported_native_ids_preserve_identity_and_raw_data(native_id, expected):
    raw = dict(FIRST, id=native_id)
    before = deepcopy(raw)
    normalized = Remotive().normalize(raw)
    assert normalized["requisition"] == str(expected)
    assert normalized["attributes"]["native_job_id"] == expected
    assert normalized["attributes"]["native_posting_id"] == expected
    assert normalized["application_url"] == FIRST["url"]
    assert raw == before


def test_numeric_id_at_conversion_boundary_still_normalizes(integer_limit):
    normalized = Remotive().normalize(dict(FIRST, id="9" * 640))
    assert normalized["requisition"] == "9" * 640


@pytest.mark.parametrize("bad_id", ["\u00b2", "9" * 641], ids=["unicode", "oversized"])
@pytest.mark.parametrize("bad_first", [True, False], ids=["invalid-first", "invalid-last"])
def test_collection_preserves_valid_jobs_and_recovers_after_reopen(
        tmp_path, integer_limit, bad_id, bad_first):
    database_url = f"sqlite:///{(tmp_path / 'source.db').as_posix()}"
    store = Store(database_url)
    store.migrate()
    store.bind_workspace_mode("FIXTURE")
    store.save_profile(PROFILE)
    source_id = configure_source(store, "default", provider="remotive", poll_interval=300)
    valid = [dict(FIRST), dict(SECOND)]
    bad = dict(FIRST, id=bad_id, title="Malformed identity fixture")
    payload = {"jobs": [bad, *valid] if bad_first else [*valid, bad]}
    requests = []

    def respond(request):
        requests.append((request.method, str(request.url)))
        assert request.method == "GET"
        assert str(request.url) == "https://remotive.com/api/remote-jobs"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(respond)
    reopened = None
    try:
        result = collect_source(store, source_id, "fixture", transport=transport)
        assert result["jobs_seen"] == 2
        assert result["rejected"] == 1
        assert result["direct_application"] is False
        with store.engine.connect() as connection:
            jobs = connection.execute(select(m.jobs)).mappings().all()
            postings = connection.execute(select(m.postings)).mappings().all()
            snapshot_ids = set(connection.execute(select(m.posting_snapshots.c.id)).scalars())
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one()
        assert {job["requisition"] for job in jobs} == {"1001", "1002"}
        assert len(postings) == len(snapshot_ids) == 2
        assert {posting["raw"]["id"] for posting in postings} == {1001, 1002}
        assert {posting["application_url"] for posting in postings} == {FIRST["url"], SECOND["url"]}
        assert source["health_status"] == "PARTIAL"
        assert source["consecutive_failures"] == 0
        assert source["last_error"] is None
        assert source["backoff_until"] is None
        job_ids = {job["id"] for job in jobs}

        store.engine.dispose()
        reopened = Store(database_url)
        with pytest.raises(SourceError, match="^SOURCE_COOLDOWN$"):
            collect_source(reopened, source_id, "fixture", transport=transport)
        # Advance only this synthetic source to its next eligible fixture poll.
        with reopened.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == source_id).values(last_attempt=None))
        retry = collect_source(reopened, source_id, "fixture", transport=transport)
        assert (retry["jobs_seen"], retry["rejected"]) == (2, 1)
        payload["jobs"] = valid
        with reopened.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == source_id).values(last_attempt=None))
        recovered = collect_source(reopened, source_id, "fixture", transport=transport)
        assert (recovered["jobs_seen"], recovered["rejected"]) == (2, 0)
        with reopened.engine.connect() as connection:
            assert set(connection.execute(select(m.jobs.c.id)).scalars()) == job_ids
            assert set(connection.execute(select(m.posting_snapshots.c.id)).scalars()) == snapshot_ids
            current = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one()
        assert current["health_status"] == "HEALTHY"
        assert current["consecutive_failures"] == 0
        assert current["last_error"] is None
        assert reopened.count("applications") == 0

        with TestClient(create_app(reopened, "http://127.0.0.1:1", tmp_path,
                                  "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
            for job_id in job_ids:
                response = client.get(f"/jobs/{job_id}")
                assert response.status_code == 200
                detail = response.json()
                assert detail["attributes"]["native_job_id"] in {1001, 1002}
                assert detail["attributes"]["submission_capability"] == "MANUAL_ONLY"
                assert detail["application_url"] in {FIRST["url"], SECOND["url"]}
        assert requests == [("GET", "https://remotive.com/api/remote-jobs")] * 3
    finally:
        if reopened is not None:
            reopened.engine.dispose()
        store.engine.dispose()


def test_all_malformed_records_remain_a_classified_source_failure(tmp_path, integer_limit):
    store = Store(f"sqlite:///{(tmp_path / 'invalid.db').as_posix()}")
    try:
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(PROFILE)
        source_id = configure_source(store, "default", provider="remotive")
        payload = {"jobs": [dict(FIRST, id="\u00b2"), dict(SECOND, id="9" * 641)]}
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        with pytest.raises(SourceError, match="^SOURCE_NO_VALID_JOBS$"):
            collect_source(store, source_id, "fixture", transport=transport)
        assert store.count(m.jobs.name) == store.count(m.postings.name) == 0
        assert store.count("posting_snapshots") == 0
        with store.engine.connect() as connection:
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one()
        assert source["health_status"] == "FAILED"
        assert source["last_error"] == "SOURCE_NO_VALID_JOBS"
    finally:
        store.engine.dispose()
