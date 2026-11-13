"""Arbeitnow's exact deterministic parser provenance survives persistent readback."""
from copy import deepcopy
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
from services.discovery.sources.arbeitnow import Arbeitnow
from services.discovery.sources.collect import collect_source, configure_source

FIXTURE = Path(__file__).parents[1] / "fixtures" / "arbeitnow.json"
TEMPLATE = "services.discovery.sources.arbeitnow.Arbeitnow.normalize"


@pytest.fixture
def store(tmp_path):
    database = Store("sqlite:///" + (tmp_path / "arbeitnow-provenance.db").as_posix())
    database.migrate()
    try:
        yield database
    finally:
        database.engine.dispose()


def first_job():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["data"][0]


def collect_one(store):
    source_id = configure_source(store, "default", provider="arbeitnow", rate_limit=600)
    raw = first_job()
    result = collect_source(store, source_id, None, refresh_matches=False,
                            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [raw]})))
    assert result["jobs_seen"] == 1
    with store.engine.connect() as connection:
        row = dict(connection.execute(select(m.posting_value_candidates)).mappings().one())
    return source_id, row


def test_known_parser_captures_exact_non_ai_manifest_without_job_inputs():
    snapshot = capture_parser(Arbeitnow())
    result = resolve_provenance(snapshot)
    assert snapshot["kind"] == "IMPLEMENTATION_MANIFEST"
    assert result["target"] == "job_parsing"
    assert result["template_id"] == TEMPLATE
    assert result["execution_kind"] == "DETERMINISTIC_PARSER"
    assert result["ai_executed"] is False
    assert result["digest_kind"] == "implementation-manifest-sha256"
    assert result["definitions"][0]["entrypoint"] == TEMPLATE
    assert any(helper["module"] == "services.discovery.sources.arbeitnow" for helper in result["helpers"])
    assert len(result["sha256"]) == 64
    serialized = json.dumps(snapshot)
    raw = first_job()
    assert raw["company_name"] not in serialized
    assert raw["slug"] not in serialized
    assert raw["description"] not in serialized


def test_persisted_parser_identity_is_historical_after_normalizer_change_and_reopen(store):
    source_id, old_row = collect_one(store)
    original_values = deepcopy(old_row["normalized"])
    original = parsing_provenance(store, old_row["id"])
    assert original["template_id"] == TEMPLATE
    normalize = Arbeitnow.normalize

    def changed_normalize(adapter, raw):
        result = normalize(adapter, raw)
        result["title"] += " revised fixture parser"
        return result

    with store.engine.begin() as connection:
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
            last_attempt="2000-01-01T00:00:00+00:00"))
    with patch.object(Arbeitnow, "normalize", changed_normalize):
        result = collect_source(store, source_id, None, refresh_matches=False,
                                transport=httpx.MockTransport(lambda _: httpx.Response(
                                    200, json={"data": [first_job()]})))
        assert result["jobs_seen"] == 1

    url = str(store.engine.url)
    store.engine.dispose()
    reopened = Store(url)
    try:
        assert parsing_provenance(reopened, old_row["id"]) == original
        with reopened.engine.connect() as connection:
            candidates = [dict(row) for row in connection.execute(
                select(m.posting_value_candidates)).mappings()]
        assert len(candidates) == 2
        previous = next(row for row in candidates if row["id"] == old_row["id"])
        newer = next(row for row in candidates if row["id"] != old_row["id"])
        assert previous["normalized"] == original_values
        new_provenance = parsing_provenance(reopened, newer["id"])
        assert new_provenance["template_id"] == TEMPLATE
        assert new_provenance["sha256"] != original["sha256"]
        assert newer["normalized"]["title"].endswith(" revised fixture parser")
    finally:
        reopened.engine.dispose()


def test_persisted_provenance_rejects_reassigned_source_binding(store):
    _, candidate = collect_one(store)
    assert parsing_provenance(store, candidate["id"])["template_id"] == TEMPLATE
    with store.engine.begin() as connection:
        connection.execute(update(m.company_sources).where(
            m.company_sources.c.id == candidate["company_source_id"]).values(provider="remotive"))
    with pytest.raises(ValueError, match="PROVENANCE_BINDING_MISMATCH"):
        parsing_provenance(store, candidate["id"])


def test_persisted_provenance_rejects_tampering_and_does_not_invent_legacy_capture(store):
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
