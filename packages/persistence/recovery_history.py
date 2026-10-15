"""Persistence for bounded, inert historical profile snapshots."""
from __future__ import annotations

import json
import math
from urllib.parse import unquote, urlsplit

from sqlalchemy import select, union

from packages.persistence import models as m
from packages.persistence.store import identity


MAX_HISTORY_ROWS = 10_000
MAX_HISTORY_ROW_BYTES = 32 * 1024
MAX_HISTORY_BYTES = 5 * 1024 * 1024
SAFE_DISCOVERY_REASON_CODES = frozenset({
    "PREVIOUSLY_UNSEEN_COMPANY", "USER_PREFERRED_COMPANY", "MATCH_ELIGIBLE_JOB", "HIGH_MATCH_JOB",
})
_FIELDS = frozenset({
    "historical_refs", "target_job_id", "target_company_id", "state", "reasons", "highest_match_score",
    "created_at", "last_seen_at", "revision", "selected_website",
    "reference_status", "metadata_status", "authorization_required",
})
_REFERENCE_FIELDS = frozenset({
    "profile_id", "company_id", "company_source_id", "candidate_id", "trigger_job_id",
})


def _safe_payload(value):
    if not isinstance(value, dict) or set(value) - _FIELDS:
        raise ValueError("invalid recovery history payload")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_HISTORY_ROW_BYTES:
        raise ValueError("recovery history snapshot is too large")
    normalized = json.loads(encoded)
    refs = normalized.get("historical_refs")
    if (not isinstance(refs, dict) or "profile_id" not in refs
            or set(refs) - _REFERENCE_FIELDS
            or any(not isinstance(item, str) or not item or len(item) > 128
                   for item in refs.values())):
        raise ValueError("invalid historical references")
    if any(normalized.get(field) is not None and
           (not isinstance(normalized[field], str) or len(normalized[field]) > 128)
           for field in ("target_job_id", "target_company_id")):
        raise ValueError("invalid remapped references")
    if (normalized.get("state") not in {"NEEDS_ORIGIN_REVIEW", "QUEUED", "DISMISSED", "UNKNOWN"}
            or not isinstance(normalized.get("reasons"), list)
            or len(normalized["reasons"]) > 50
            or any(item not in SAFE_DISCOVERY_REASON_CODES for item in normalized["reasons"])
            or normalized.get("reference_status") not in {"LINKED", "JOB_NOT_IN_BACKUP", "NO_JOB_REFERENCE"}
            or normalized.get("metadata_status") not in {"COMPLETE", "PARTIAL"}
            or normalized.get("authorization_required") is not True):
        raise ValueError("invalid historical snapshot")
    score = normalized.get("highest_match_score")
    if score is not None and (type(score) not in (int, float) or not math.isfinite(score)):
        raise ValueError("invalid historical score")
    revision = normalized.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("invalid historical revision")
    for field in ("created_at", "last_seen_at"):
        if not isinstance(normalized.get(field), str) or len(normalized[field]) > 64:
            raise ValueError("invalid historical timestamp")
    website = normalized.get("selected_website")
    if website is not None and (not isinstance(website, str) or len(website) > 2000):
        raise ValueError("invalid historical website")
    if website is not None:
        try:
            parsed = urlsplit(website)
            sensitive = {"token", "session", "auth", "password", "secret", "credential", "access_key"}
            if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment
                    or any(segment.casefold() in sensitive
                           for segment in unquote(parsed.path).split("/"))):
                raise ValueError("unsafe historical website")
        except ValueError:
            raise ValueError("unsafe historical website") from None
    if normalized.get("reference_status") == "LINKED" and not normalized.get("target_job_id"):
        raise ValueError("invalid historical job reference")
    return normalized


def insert_recovery_history(connection, profile_id: str, rows: list[dict]) -> None:
    """Insert inert snapshots in the caller's restore transaction."""
    if len(rows) > MAX_HISTORY_ROWS:
        raise ValueError("too many recovery history snapshots")
    total = 0
    seen = set()
    for row in rows:
        kind = row.get("kind")
        source_identity = row.get("source_identity")
        if (kind != "company_discovery_request" or not isinstance(source_identity, str)
                or not source_identity or len(source_identity) > 128
                or source_identity in seen or type(row.get("schema_version")) is not int
                or row.get("schema_version") != 1):
            raise ValueError("invalid recovery history identity")
        if not isinstance(row.get("created_at"), str) or len(row["created_at"]) > 64:
            raise ValueError("invalid recovery history timestamp")
        seen.add(source_identity)
        payload = _safe_payload(row.get("payload"))
        total += len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if total > MAX_HISTORY_BYTES:
            raise ValueError("recovery history is too large")
        connection.execute(m.profile_recovery_history_snapshots.insert().values(
            id=identity("profile-recovery-history", profile_id, kind, source_identity),
            profile_id=profile_id, kind=kind, source_identity=source_identity,
            schema_version=1, payload=payload, created_at=row.get("created_at") or "",
        ))


def read_recovery_history(connection, profile_id: str) -> list[dict]:
    """Read only the fixed, inert recovery history projection for one profile."""
    profile_job_ids = union(
        select(m.matches.c.job_id).where(m.matches.c.profile_id == profile_id),
        select(m.applications.c.job_id).where(m.applications.c.profile_id == profile_id),
    ).subquery("recovery_history_profile_jobs")
    owned_jobs = {row[0] for row in connection.execute(
        select(profile_job_ids.c.job_id)).all()}
    owned_companies = ({row[0] for row in connection.execute(select(m.routes.c.company_id).where(
        m.routes.c.job_id.in_(owned_jobs))).all()} if owned_jobs else set())
    rows = connection.execute(select(
        m.profile_recovery_history_snapshots.c.kind,
        m.profile_recovery_history_snapshots.c.source_identity,
        m.profile_recovery_history_snapshots.c.schema_version,
        m.profile_recovery_history_snapshots.c.payload,
        m.profile_recovery_history_snapshots.c.created_at,
    ).where(m.profile_recovery_history_snapshots.c.profile_id == profile_id)
      .order_by(m.profile_recovery_history_snapshots.c.created_at,
                m.profile_recovery_history_snapshots.c.source_identity)
      .limit(MAX_HISTORY_ROWS + 1)).mappings().all()
    if len(rows) > MAX_HISTORY_ROWS:
        raise ValueError("recovery history exceeds read limit")
    result = []
    total = 0
    for row in rows:
        if (row["kind"] != "company_discovery_request"
                or type(row["schema_version"]) is not int or row["schema_version"] != 1
                or not isinstance(row["source_identity"], str) or not row["source_identity"]
                or len(row["source_identity"]) > 128):
            raise ValueError("corrupt recovery history identity")
        payload = _safe_payload(row["payload"])
        if (payload.get("target_job_id") is not None
                and payload["target_job_id"] not in owned_jobs):
            raise ValueError("corrupt recovery history job reference")
        if (payload.get("target_company_id") is not None
                and payload["target_company_id"] not in owned_companies):
            raise ValueError("corrupt recovery history company reference")
        total += len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if total > MAX_HISTORY_BYTES:
            raise ValueError("recovery history exceeds read limit")
        result.append({"kind": row["kind"], "source_identity": row["source_identity"],
                       "schema_version": row["schema_version"], "payload": payload,
                       "created_at": row["created_at"]})
    return result
