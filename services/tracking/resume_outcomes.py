"""User-reported resume-version lineage and observational application outcomes.

These records never submit an application or infer a provider response. A binding
is an explicit report of the document used, not proof of employer receipt.
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.resume_outcomes import bindings, events
from packages.persistence.store import identity, now

OUTCOME_KINDS = frozenset({"RESPONSE", "SCREEN", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"})
_CATEGORIES = ("response", "screen", "interview", "outcome")
_SPARSE_MINIMUM = 5


def _utc(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Observation time must be a bounded UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Observation time must be an ISO UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("Observation time must be UTC")
    return parsed.isoformat()


def _request(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64 or value != value.strip():
        raise ValueError("Request identifier must have 1 to 64 non-padded characters")
    return value


def _public_binding(row) -> dict:
    return {
        "application_id": row["application_id"],
        "document_id": row["document_id"],
        "document_version": row["document_version"],
        "reported_at": row["reported_at"],
        "attribution": "USER_REPORTED",
    }


def report_submitted_resume_version(store, profile_id: str, application_id: str,
                                    document_id: str, reported_at: str, request_id: str) -> dict:
    """Pin one explicitly reported resume document per application, without submitting."""
    reported_at = _utc(reported_at)
    request_id = _request(request_id)
    store.profile(profile_id)
    binding_id = identity("resume-outcome-binding", profile_id, application_id)
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == application_id).with_for_update()).mappings().one_or_none()
        if application is None or application["profile_id"] != profile_id:
            raise LookupError("Application is unavailable for this profile")
        existing = connection.execute(select(bindings).where(
            bindings.c.id == binding_id)).mappings().one_or_none()
        if existing is not None:
            if (existing["document_id"] != document_id
                    or existing["reported_at"] != reported_at
                    or existing["request_id"] != request_id):
                raise ValueError("Reported resume version is already bound")
            return _public_binding(existing)
        document = connection.execute(select(m.documents).where(
            m.documents.c.id == document_id,
            m.documents.c.application_id == application_id,
            m.documents.c.kind == "resume")).mappings().one_or_none()
        if document is None:
            raise ValueError("Choose a prepared resume document for this application")
        values = {
            "id": binding_id, "profile_id": profile_id,
            "application_id": application_id, "document_id": document_id,
            "document_version": document["version"],
            "document_sha256": document["sha256"],
            "reported_at": reported_at, "request_id": request_id,
            "created_at": now(),
        }
        connection.execute(bindings.insert().values(**values))
        return _public_binding(values)


def record_observed_outcome(store, profile_id: str, application_id: str,
                            kind: str, observed_at: str, request_id: str) -> dict:
    """Append a timestamped user observation for an already pinned version."""
    if kind not in OUTCOME_KINDS:
        raise ValueError("Unsupported observed outcome")
    observed_at = _utc(observed_at)
    request_id = _request(request_id)
    store.profile(profile_id)
    binding_id = identity("resume-outcome-binding", profile_id, application_id)
    event_id = identity("resume-outcome-event", binding_id, request_id)
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        binding = connection.execute(select(bindings).where(
            bindings.c.id == binding_id,
            bindings.c.profile_id == profile_id)).mappings().one_or_none()
        if binding is None:
            raise LookupError("Reported resume version is unavailable for this profile")
        existing = connection.execute(select(events).where(events.c.id == event_id)).mappings().one_or_none()
        if existing is not None:
            if existing["kind"] != kind or existing["observed_at"] != observed_at:
                raise ValueError("Outcome request identifier was already used for different content")
        else:
            connection.execute(events.insert().values(
                id=event_id, binding_id=binding_id, kind=kind,
                observed_at=observed_at, request_id=request_id, created_at=now()))
    return {"application_id": application_id, "document_id": binding["document_id"],
            "kind": kind, "observed_at": observed_at, "attribution": "USER_REPORTED"}


def analyze_resume_outcomes(store, profile_id: str, since: str | None = None,
                            until: str | None = None) -> dict:
    """Count observed milestones by pinned document; absent evidence stays unknown."""
    since = _utc(since) if since is not None else None
    until = _utc(until) if until is not None else None
    if since is not None and until is not None and since > until:
        raise ValueError("Time window starts after it ends")
    store.profile(profile_id)
    with store.read_snapshot() as connection:
        condition = [bindings.c.profile_id == profile_id]
        if since is not None:
            condition.append(bindings.c.reported_at >= since)
        if until is not None:
            condition.append(bindings.c.reported_at <= until)
        selected = list(connection.execute(select(bindings).where(*condition)).mappings())
        event_rows = list(connection.execute(select(events).where(
            events.c.binding_id.in_([row["id"] for row in selected]),
            *((events.c.observed_at >= since,) if since is not None else ()),
            *((events.c.observed_at <= until,) if until is not None else ()),
        )).mappings()) if selected else []

    observed = {row["id"]: set() for row in selected}
    for row in event_rows:
        observed[row["binding_id"]].add(
            row["kind"].lower() if row["kind"] in {"RESPONSE", "SCREEN", "INTERVIEW"}
            else "outcome")
    grouped = {}
    for row in selected:
        item = grouped.setdefault(row["document_id"], {
            "document_id": row["document_id"],
            "document_version": row["document_version"],
            "sample_size": 0,
            "counts": {key: 0 for key in _CATEGORIES},
        })
        item["sample_size"] += 1
        for category in observed[row["id"]]:
            item["counts"][category] += 1
    versions = []
    for key in sorted(grouped):
        item = grouped[key]
        item["unknown"] = {category: item["sample_size"] - item["counts"][category]
                           for category in _CATEGORIES}
        item["rates"] = {category: item["counts"][category] / item["sample_size"]
                         for category in _CATEGORIES}
        item["warnings"] = ["SPARSE_SAMPLE"] if item["sample_size"] < _SPARSE_MINIMUM else []
        versions.append(item)
    totals = {"counts": {category: sum(item["counts"][category] for item in versions)
                          for category in _CATEGORIES}}
    totals["unknown"] = {category: len(selected) - totals["counts"][category]
                         for category in _CATEGORIES}
    totals["rates"] = {category: (totals["counts"][category] / len(selected)
                                  if selected else None)
                       for category in _CATEGORIES}
    return {"profile_id": profile_id, "since": since, "until": until,
            "sample_size": len(selected), "interpretation": "OBSERVATIONAL",
            "warnings": ["SELECTION_BIAS", *(["SPARSE_SAMPLE"]
                        if len(selected) < _SPARSE_MINIMUM else [])],
            "totals": totals, "versions": versions}
