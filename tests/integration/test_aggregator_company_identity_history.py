"""Curated company names and legacy source evidence retain separate identities."""
import hashlib
import json
from copy import deepcopy
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.sources import collect as collector
from services.discovery.sources.collect import configure_source
from services.discovery.sources.errors import SourceError
from tests.integration import test_aggregator_company_identity as identity_contract
from tests.integration.test_aggregator_company_identity import (
    CONFLICT,
    PROVIDERS,
    collect_names,
    domain_snapshot,
    release_cooldown,
)

ACTION = "AGGREGATOR_COMPANY_LABEL_BOUND"
REVIEW = "SOURCE_COMPANY_IDENTITY_REVIEW_REQUIRED"
store = identity_contract.store


def receipt_for(database, provider, company_id):
    with database.engine.connect() as connection:
        row = connection.execute(select(m.audit).where(
            m.audit.c.id == identity("aggregator-company-label-v1", provider, company_id),
        )).mappings().one_or_none()
    return dict(row) if row is not None else None


def one_company(database):
    with database.engine.connect() as connection:
        return dict(connection.execute(select(m.companies)).mappings().one())


def legacy_collect(database, provider, names, *, source_id=None, first_id=8100):
    """Exercise real source persistence without the newly introduced label receipt."""
    if source_id is not None:
        release_cooldown(database, source_id)
    with patch.object(collector, "_retain_company_label", return_value=None):
        return collect_names(
            database, provider, names, source_id=source_id, first_id=first_id,
        )


def raw_checksum(raw):
    return hashlib.sha256(json.dumps(
        raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def assert_refusal_preserves_history(database, provider, source_id, name, error):
    release_cooldown(database, source_id)
    before = domain_snapshot(database)
    company_id = one_company(database)["id"]
    old_receipt = receipt_for(database, provider, company_id)
    with pytest.raises(SourceError, match="^" + error + "$"):
        collect_names(database, provider, [name], source_id=source_id, first_id=9900)
    assert domain_snapshot(database) == before
    assert receipt_for(database, provider, company_id) == old_receipt
    with database.engine.connect() as connection:
        source = connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == source_id,
        )).mappings().one()
    assert source["last_error"] == error


@pytest.mark.parametrize("provider", PROVIDERS)
def test_first_upstream_receipt_preserves_preseeded_curated_name_and_domain(store, provider):
    company_id = identity(provider, "example-employer")
    with store.engine.begin() as connection:
        connection.execute(m.companies.insert().values(
            id=company_id, name="Curated Employer Name", domain="https://employer.example",
        ))
    _, result = collect_names(store, provider, ["Example Employer", "New Employer"])
    assert result["jobs_seen"] == 2
    with store.engine.connect() as connection:
        curated = connection.execute(select(m.companies).where(
            m.companies.c.id == company_id,
        )).mappings().one()
        companies = connection.execute(select(m.companies)).mappings().all()
    assert curated["name"] == "Curated Employer Name"
    assert curated["domain"] == "https://employer.example"
    assert len(companies) == 2
    receipt = receipt_for(store, provider, company_id)
    assert receipt["entity_id"] == company_id
    assert receipt["action"] == ACTION
    assert receipt["payload"]["provider"] == provider
    assert receipt["payload"]["version"] == 1
    assert receipt["payload"]["legacy_snapshot_ids"] == []
    assert "Example Employer" not in json.dumps(receipt)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_later_curation_and_reopen_preserve_receipt_and_reject_conflicting_label(store, provider):
    source_id, _ = collect_names(store, provider, ["Synthetic A+B"])
    company_id = one_company(store)["id"]
    original_receipt = receipt_for(store, provider, company_id)
    with store.engine.begin() as connection:
        connection.execute(update(m.companies).where(m.companies.c.id == company_id).values(
            name="Operator Display Name", domain="https://reviewed.example",
        ))
    database_url = store.engine.url.render_as_string(hide_password=False)
    store.engine.dispose()
    reopened = Store(database_url)
    try:
        release_cooldown(reopened, source_id)
        _, result = collect_names(reopened, provider, ["Synthetic A+B"], source_id=source_id)
        assert result["jobs_seen"] == 1
        assert receipt_for(reopened, provider, company_id) == original_receipt
        company = one_company(reopened)
        assert company["name"] == "Operator Display Name"
        assert company["domain"] == "https://reviewed.example"
        with reopened.engine.connect() as connection:
            assert len(connection.execute(select(m.jobs.c.id)).all()) == 1
        assert_refusal_preserves_history(
            reopened, provider, source_id, "Synthetic A B", CONFLICT,
        )
    finally:
        reopened.engine.dispose()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_legacy_bootstrap_uses_full_raw_label_beyond_truncated_display(store, provider):
    original = "A" * 205 + "+First"
    conflicting = "A" * 205 + " First"
    source_id, _ = legacy_collect(store, provider, [original])
    company_id = one_company(store)["id"]
    assert receipt_for(store, provider, company_id) is None
    with store.engine.begin() as connection:
        normalized = connection.execute(select(m.posting_value_candidates.c.normalized)).scalar_one()
        snapshot_ids = set(connection.execute(select(m.posting_snapshots.c.id)).scalars())
        assert normalized["attributes"]["company_name"] == original[:200] == conflicting[:200]
        connection.execute(update(m.companies).where(m.companies.c.id == company_id).values(
            name="Reviewed Legacy Display", domain="https://legacy.example",
        ))
    release_cooldown(store, source_id)
    collect_names(store, provider, [original], source_id=source_id)
    receipt = receipt_for(store, provider, company_id)
    assert set(receipt["payload"]["legacy_snapshot_ids"]) == snapshot_ids
    assert one_company(store)["name"] == "Reviewed Legacy Display"
    assert one_company(store)["domain"] == "https://legacy.example"
    assert_refusal_preserves_history(store, provider, source_id, conflicting, CONFLICT)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("reverse", (False, True), ids=("forward", "reverse"))
def test_ambiguous_legacy_labels_refuse_bootstrap_in_both_observation_orders(store, provider, reverse):
    names = ["Synthetic A+B", "Synthetic A B"]
    if reverse:
        names.reverse()
    source_id, _ = legacy_collect(store, provider, [names[0]])
    legacy_collect(store, provider, [names[1]], source_id=source_id, first_id=8200)
    assert receipt_for(store, provider, one_company(store)["id"]) is None
    assert_refusal_preserves_history(store, provider, source_id, names[0], CONFLICT)


@pytest.mark.parametrize(
    ("snapshot_count", "repeat"),
    ((64, False), (64, True), (65, False)),
    ids=("64_snapshots", "64_snapshots_repeated_candidates", "65_snapshots"),
)
def test_legacy_recovery_bound_counts_distinct_snapshots_not_candidates(store, snapshot_count, repeat):
    provider = "wwr"
    name = "Bounded Legacy Employer"
    source_id, _ = legacy_collect(store, provider, [name] * snapshot_count)
    if repeat:
        legacy_collect(store, provider, [name] * snapshot_count, source_id=source_id)
    company_id = one_company(store)["id"]
    with store.engine.connect() as connection:
        snapshots = set(connection.execute(select(m.posting_snapshots.c.id)).scalars())
        candidates = connection.execute(select(m.posting_value_candidates.c.id)).all()
    assert len(snapshots) == snapshot_count
    assert len(candidates) == snapshot_count * (2 if repeat else 1)
    if snapshot_count == 65:
        assert_refusal_preserves_history(store, provider, source_id, name, REVIEW)
        return
    release_cooldown(store, source_id)
    collect_names(store, provider, [name], source_id=source_id)
    receipt = receipt_for(store, provider, company_id)
    assert set(receipt["payload"]["legacy_snapshot_ids"]) == snapshots
    assert len(receipt["payload"]["legacy_snapshot_ids"]) == 64


@pytest.mark.parametrize(
    "damage",
    (
        "checksum",
        "candidate_provider",
        "raw_type",
        "candidate_posting",
        "candidate_snapshot",
        "source_company",
        "snapshot_identity",
    ),
)
def test_malformed_legacy_provenance_requires_review_without_overwriting_history(store, damage):
    provider = "wwr"
    name = "Synthetic A+B"
    source_id, _ = legacy_collect(store, provider, [name, name])
    with store.engine.begin() as connection:
        snapshots = connection.execute(select(m.posting_snapshots).order_by(
            m.posting_snapshots.c.id,
        )).mappings().all()
        snapshot = snapshots[0]
        candidate = connection.execute(select(m.posting_value_candidates).where(
            m.posting_value_candidates.c.posting_snapshot_id == snapshot["id"],
        )).mappings().one()
        if damage == "checksum":
            connection.execute(update(m.posting_snapshots).where(
                m.posting_snapshots.c.id == snapshot["id"],
            ).values(sha256="0" * 64))
        elif damage == "candidate_provider":
            normalized = deepcopy(candidate["normalized"])
            normalized["attributes"]["provider"] = "remoteok"
            connection.execute(update(m.posting_value_candidates).where(
                m.posting_value_candidates.c.id == candidate["id"],
            ).values(normalized=normalized))
        elif damage == "raw_type":
            raw = ["malformed synthetic source record"]
            connection.execute(update(m.posting_snapshots).where(
                m.posting_snapshots.c.id == snapshot["id"],
            ).values(raw=raw, sha256=raw_checksum(raw)))
        elif damage == "candidate_posting":
            connection.execute(update(m.posting_value_candidates).where(
                m.posting_value_candidates.c.id == candidate["id"],
            ).values(posting_id=snapshots[1]["posting_id"]))
        elif damage == "candidate_snapshot":
            connection.execute(update(m.posting_value_candidates).where(
                m.posting_value_candidates.c.id == candidate["id"],
            ).values(posting_snapshot_id=snapshots[1]["id"]))
        elif damage == "source_company":
            company_id = identity("synthetic", "unrelated-company")
            connection.execute(m.companies.insert().values(
                id=company_id, name="Unrelated Synthetic Company", domain="",
            ))
            connection.execute(update(m.company_sources).where(
                m.company_sources.c.id == candidate["company_source_id"],
            ).values(company_id=company_id))
        else:
            # A checksum change cannot silently rewrite the immutable snapshot ID.
            raw = deepcopy(snapshot["raw"])
            raw["description"] = "Altered synthetic history with a recomputed checksum."
            connection.execute(update(m.posting_snapshots).where(
                m.posting_snapshots.c.id == snapshot["id"],
            ).values(raw=raw, sha256=raw_checksum(raw)))
    release_cooldown(store, source_id)
    before = domain_snapshot(store)
    with pytest.raises(SourceError, match="^" + REVIEW + "$"):
        collect_names(store, provider, [name], source_id=source_id)
    assert domain_snapshot(store) == before
    with store.engine.connect() as connection:
        assert not connection.execute(select(m.audit.c.id).where(
            m.audit.c.action == ACTION,
        )).first()


@pytest.mark.parametrize("retarget_source_url", (False, True), ids=("original_origin", "retargeted_origin"))
def test_snapshotless_historical_posting_requires_review_even_with_other_valid_history(
    store, retarget_source_url,
):
    provider = "wwr"
    name = "Synthetic Legacy Employer"
    source_id, _ = collect_names(store, provider, [name])
    company_id = one_company(store)["id"]
    receipt = receipt_for(store, provider, company_id)
    with store.engine.begin() as connection:
        connection.execute(delete(m.audit).where(m.audit.c.id == receipt["id"]))
        posting = dict(connection.execute(select(m.postings)).mappings().one())
        posting["id"] = identity("synthetic-orphan-posting", posting["id"])
        posting["source_job_id"] = "synthetic-orphan-" + posting["source_job_id"]
        if retarget_source_url:
            posting["source_url"] = "https://synthetic-unrelated.example/jobs.xml"
        connection.execute(m.postings.insert().values(**posting))
    assert_refusal_preserves_history(store, provider, source_id, name, REVIEW)


def test_downstream_candidate_failure_rolls_back_new_label_receipt_and_domain_rows(store):
    provider = "wwr"
    source_id = configure_source(store, "default", provider=provider, rate_limit=600)
    before = domain_snapshot(store)
    with (
        patch.object(collector, "record_candidate", side_effect=RuntimeError(
            "SYNTHETIC_DOWNSTREAM_CANDIDATE_FAILURE",
        )),
        pytest.raises(RuntimeError, match="^SYNTHETIC_DOWNSTREAM_CANDIDATE_FAILURE$"),
    ):
        collect_names(store, provider, ["Synthetic Atomic Employer"], source_id=source_id)
    assert domain_snapshot(store) == before
    with store.engine.connect() as connection:
        assert not connection.execute(select(m.audit.c.id).where(
            m.audit.c.action == ACTION,
        )).first()
    release_cooldown(store, source_id)
    collect_names(store, provider, ["Synthetic Atomic Employer"], source_id=source_id)
    assert receipt_for(store, provider, one_company(store)["id"]) is not None


@pytest.mark.parametrize(
    "damage", ("wrong_action", "string_version", "nonhex_hash", "invalid_snapshot_ids"),
)
def test_malformed_existing_receipt_requires_review_without_legacy_rebootstrap(store, damage):
    provider = "wwr"
    name = "Synthetic Receipt Employer"
    source_id, _ = collect_names(store, provider, [name])
    company_id = one_company(store)["id"]
    receipt = receipt_for(store, provider, company_id)
    payload = deepcopy(receipt["payload"])
    values = {}
    if damage == "wrong_action":
        values["action"] = "UNRELATED_SYNTHETIC_AUDIT"
    elif damage == "string_version":
        payload["version"] = "1"
    elif damage == "nonhex_hash":
        payload["label_sha256"] = "g" * 64
    else:
        payload["legacy_snapshot_ids"] = {"unexpected": "mapping"}
    values["payload"] = payload
    with store.engine.begin() as connection:
        connection.execute(update(m.audit).where(m.audit.c.id == receipt["id"]).values(**values))
    damaged_receipt = receipt_for(store, provider, company_id)
    assert_refusal_preserves_history(store, provider, source_id, name, REVIEW)
    assert receipt_for(store, provider, company_id) == damaged_receipt
