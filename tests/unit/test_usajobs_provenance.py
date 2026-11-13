"""USAJOBS joined postings retain exact, historical deterministic parser provenance."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.prompt_provenance import parsing_provenance
from packages.persistence.store import Store
from services.ai.prompt_provenance import capture_parser, resolve_provenance
from services.discovery.sources import usajobs
from services.discovery.sources.collect import collect_source, configure_source

FIXTURES = Path(__file__).parents[1] / "fixtures"
TEMPLATE = "services.discovery.sources.usajobs.USAJobs.normalize"
METADATA_URL = "https://data.usajobs.gov/api/historicjoa"
TEXT_URL = "https://data.usajobs.gov/api/historicjoa/announcementtext"


class FixtureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 11, 13, 12, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fixture_calendar(monkeypatch):
    monkeypatch.setattr(usajobs, "datetime", FixtureDateTime)


@pytest.fixture
def store(tmp_path):
    database = Store("sqlite:///" + (tmp_path / "usajobs-provenance.db").as_posix())
    database.migrate()
    try:
        yield database
    finally:
        database.engine.dispose()


def raw_envelope():
    metadata = json.loads((FIXTURES / "usajobs_metadata.json").read_text(encoding="utf-8"))["data"][0]
    texts = json.loads((FIXTURES / "usajobs_announcement_text.json").read_text(encoding="utf-8"))["data"]
    return {"metadata": metadata, "announcement_text": next(
        row for row in texts if row["usajobsControlNumber"] == metadata["usajobsControlNumber"])}


def fixture_transport():
    raw = raw_envelope()

    def respond(request):
        assert request.method == "GET"
        endpoint = str(request.url).partition("?")[0]
        assert endpoint in {METADATA_URL, TEXT_URL}
        if endpoint == METADATA_URL:
            return httpx.Response(200, json={"data": [raw["metadata"]]})
        assert dict(request.url.params) == {
            "USAJOBSControlNumbers": str(raw["metadata"]["usajobsControlNumber"])}
        return httpx.Response(200, json={"data": [raw["announcement_text"]]})
    return httpx.MockTransport(respond)


def collect_one(store):
    source_id = configure_source(store, "default", provider="usajobs", rate_limit=600)
    result = collect_source(store, source_id, None, refresh_matches=False,
                            transport=fixture_transport())
    assert result["jobs_seen"] == 1
    with store.engine.connect() as connection:
        candidate = dict(connection.execute(select(m.posting_value_candidates)).mappings().one())
        snapshot = dict(connection.execute(select(m.posting_snapshots).where(
            m.posting_snapshots.c.id == candidate["posting_snapshot_id"])).mappings().one())
    assert snapshot["raw"] == raw_envelope()
    return source_id, candidate


def test_exact_known_parser_manifest_excludes_upstream_metadata_and_announcement_text():
    snapshot = capture_parser(usajobs.USAJobs())
    result = resolve_provenance(snapshot)
    assert snapshot["kind"] == "IMPLEMENTATION_MANIFEST"
    assert result["target"] == "job_parsing"
    assert result["template_id"] == TEMPLATE
    assert result["execution_kind"] == "DETERMINISTIC_PARSER"
    assert result["ai_executed"] is False
    assert result["digest_kind"] == "implementation-manifest-sha256"
    assert result["definitions"][0]["entrypoint"] == TEMPLATE
    assert any(helper["module"] == "services.discovery.sources.usajobs" for helper in result["helpers"])
    assert len(result["sha256"]) == 64
    serialized = json.dumps(snapshot)
    raw = raw_envelope()
    assert str(raw["metadata"]["usajobsControlNumber"]) not in serialized
    assert raw["metadata"]["hiringAgencyName"] not in serialized
    assert raw["announcement_text"]["summary"] not in serialized
    assert raw["announcement_text"]["duties"] not in serialized


def test_historical_parser_identity_survives_changed_normalizer_and_database_reopen(store):
    source_id, old_row = collect_one(store)
    original_values = deepcopy(old_row["normalized"])
    original = parsing_provenance(store, old_row["id"])
    assert original["template_id"] == TEMPLATE
    normalize = usajobs.USAJobs.normalize

    def changed_normalize(adapter, raw):
        result = normalize(adapter, raw)
        result["title"] += " revised fixture parser"
        return result

    with store.engine.begin() as connection:
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
            last_attempt="2000-01-01T00:00:00+00:00"))
    with patch.object(usajobs.USAJobs, "normalize", changed_normalize):
        result = collect_source(store, source_id, None, refresh_matches=False,
                                transport=fixture_transport())
        assert result["jobs_seen"] == 1

    url = str(store.engine.url)
    store.engine.dispose()
    reopened = Store(url)
    try:
        assert parsing_provenance(reopened, old_row["id"]) == original
        with reopened.engine.connect() as connection:
            candidates = [dict(row) for row in connection.execute(
                select(m.posting_value_candidates)).mappings()]
            snapshots = [dict(row) for row in connection.execute(
                select(m.posting_snapshots)).mappings()]
        assert len(candidates) == 2
        previous = next(row for row in candidates if row["id"] == old_row["id"])
        newer = next(row for row in candidates if row["id"] != old_row["id"])
        assert previous["normalized"] == original_values
        assert newer["posting_snapshot_id"] == previous["posting_snapshot_id"]
        assert len(snapshots) == 1 and snapshots[0]["raw"] == raw_envelope()
        current_provenance = parsing_provenance(reopened, newer["id"])
        assert current_provenance["template_id"] == TEMPLATE
        assert current_provenance["sha256"] != original["sha256"]
        assert newer["normalized"]["title"].endswith(" revised fixture parser")
    finally:
        reopened.engine.dispose()


@pytest.mark.parametrize("provider", ["arbeitnow", "unregistered"])
def test_persisted_parser_readback_rejects_unrelated_or_unknown_provider(store, provider):
    # The captured producer is bound to a provider implementation, not tenant data.
    _, candidate = collect_one(store)
    assert parsing_provenance(store, candidate["id"])["template_id"] == TEMPLATE
    with store.engine.begin() as connection:
        connection.execute(update(m.company_sources).where(
            m.company_sources.c.id == candidate["company_source_id"]).values(provider=provider))
    with pytest.raises(ValueError, match="PROVENANCE_BINDING_MISMATCH"):
        parsing_provenance(store, candidate["id"])


def test_tampered_or_missing_capture_never_becomes_valid_historical_provenance(store):
    _, candidate = collect_one(store)
    normalized = deepcopy(candidate["normalized"])
    tampered = deepcopy(normalized)
    tampered["attributes"]["prompt_provenance"]["definition"]["sha256"] = "0" * 64
    with store.engine.begin() as connection:
        connection.execute(update(m.posting_value_candidates).where(
            m.posting_value_candidates.c.id == candidate["id"]).values(normalized=tampered))
    with pytest.raises(ValueError):
        parsing_provenance(store, candidate["id"])
    normalized["attributes"].pop("prompt_provenance")
    with store.engine.begin() as connection:
        connection.execute(update(m.posting_value_candidates).where(
            m.posting_value_candidates.c.id == candidate["id"]).values(normalized=normalized))
    with pytest.raises(ValueError, match="PROVENANCE_UNRECORDED"):
        parsing_provenance(store, candidate["id"])
