"""Passive, profile-scoped catalogue for user-supplied job source links."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, update
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert


class SourceCandidateConflict(ValueError):
    """A review request identifier was reused with different content."""


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if not host or (port is not None and not 1 <= port <= 65535):
        return None
    netloc = host if port is None else f"{host}:{port}"
    segments = parsed.path.split("/")
    sensitive_segments = {"token", "session", "auth", "password", "secret", "credential", "access_key"}
    for index, segment in enumerate(segments[:-1]):
        if segment.casefold() in sensitive_segments:
            segments[index + 1] = "[REDACTED]"
    path = "/".join(segments)[:1500]
    return urlunsplit((parsed.scheme.casefold(), netloc, path, "", ""))[:2000]


def _domain(value: str | None) -> str | None:
    safe = _safe_url(value)
    if not safe:
        return None
    host = urlsplit(safe).hostname
    return host[4:] if host and host.startswith("www.") else host


def _origin(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:253] or None


def record_capture(connection, *, profile_id: str | None, request_id: str,
                   job_id: str | None, application_id: str | None,
                   discovery_origin: str | None, source_url: str | None,
                   application_url: str | None, observed_hops=None,
                   capture_method: str, origin_attribution: str | None = None,
                   evidence_status: str | None = None):
    """Record one sanitized provenance observation without opening any URL."""
    if not profile_id:
        return None
    safe_source = _safe_url(source_url)
    safe_application = _safe_url(application_url)
    candidate_url = safe_application or safe_source
    domain = _domain(candidate_url)
    if not domain:
        return None
    timestamp = now()
    candidate_id = identity("source-candidate", profile_id, domain)
    if capture_method not in {"MANUAL_CAPTURE", "EXTERNAL_APPLICATION", "SUPPORTED_COLLECTION"}:
        raise ValueError("SOURCE_CANDIDATE_CAPTURE_METHOD_INVALID")
    capture_key = f"{capture_method}:{request_id}"
    observation_id = identity("source-candidate-observation", profile_id, capture_key)
    origin = _origin(discovery_origin)
    hops = []
    hops_input = observed_hops if isinstance(observed_hops, (list, tuple)) else []
    for item in hops_input:
        if isinstance(item, str) and len(hops) < 20:
            hops.append(_safe_url(item) or {"state": "UNKNOWN"})
    if not hops:
        hops = [{"state": "UNKNOWN"}]
    attribution = origin_attribution or ("USER_REPORTED" if origin else "UNKNOWN")
    status = evidence_status or (
        "USER_REPORTED" if origin or safe_source or safe_application else "UNKNOWN")
    if attribution not in {"USER_REPORTED", "UNKNOWN"} or status not in {
            "USER_REPORTED", "PARTIAL", "UNKNOWN"}:
        raise ValueError("SOURCE_CANDIDATE_EVIDENCE_STATUS_INVALID")

    upsert(connection, m.source_candidates, {
        "id": candidate_id, "profile_id": profile_id, "domain": domain,
        "display_name": domain, "site_type": "UNKNOWN", "state": "NEEDS_REVIEW",
        "first_seen_at": timestamp, "last_seen_at": timestamp,
        "collection_enabled": False, "review_reason": None, "integration_issue": None,
    })
    connection.execute(update(m.source_candidates).where(
        m.source_candidates.c.id == candidate_id
    ).values(last_seen_at=timestamp))
    previous = connection.execute(select(m.source_candidate_observations).where(
        m.source_candidate_observations.c.id == observation_id
    )).mappings().one_or_none()
    if previous is not None:
        expected = (candidate_id, job_id, application_id, origin,
                    "USER_REPORTED" if origin else "UNKNOWN", safe_source,
                    safe_application, hops, capture_method)
        actual = (previous["candidate_id"], previous["job_id"], previous["application_id"],
                  previous["discovery_origin"], previous["origin_attribution"],
                  previous["source_url"], previous["application_url"],
                  previous["observed_hops"], previous["capture_method"])
        if actual != expected:
            raise SourceCandidateConflict("Source candidate capture identifier was reused for different provenance")
    upsert(connection, m.source_candidate_observations, {
        "id": observation_id, "candidate_id": candidate_id, "profile_id": profile_id,
        "capture_key": capture_key, "job_id": job_id, "application_id": application_id,
        "discovery_origin": origin,
        "origin_attribution": attribution,
        "source_url": safe_source, "application_url": safe_application,
        "observed_hops": hops, "capture_method": capture_method,
        "evidence_status": status,
        "observed_at": timestamp,
    })
    return candidate_id


def record_supported_collection(connection, *, profile_id: str, request_id: str,
                                job_id: str, source_url: str,
                                application_url: str):
    """Record feed-observed third-party destinations without claiming redirects or attribution."""
    safe_source = _safe_url(source_url)
    safe_application = _safe_url(application_url)
    if (not safe_source or not safe_application
            or _domain(safe_source) == _domain(safe_application)):
        return None
    return record_capture(
        connection, profile_id=profile_id, request_id=request_id, job_id=job_id,
        application_id=None, discovery_origin=None, source_url=safe_source,
        application_url=safe_application, observed_hops=[{"state": "UNKNOWN"}],
        capture_method="SUPPORTED_COLLECTION", origin_attribution="UNKNOWN",
        evidence_status="PARTIAL",
    )


def list_candidates(store, profile_id: str):
    store.profile(profile_id)
    with store.engine.connect() as connection:
        candidates = [dict(row) for row in connection.execute(
            select(m.source_candidates).where(
                m.source_candidates.c.profile_id == profile_id
            ).order_by(m.source_candidates.c.last_seen_at.desc(),
                       m.source_candidates.c.domain)
        ).mappings()]
        for candidate in candidates:
            candidate["observations"] = [dict(row) for row in connection.execute(
                select(m.source_candidate_observations).where(
                    m.source_candidate_observations.c.candidate_id == candidate["id"],
                    m.source_candidate_observations.c.profile_id == profile_id,
                ).order_by(m.source_candidate_observations.c.observed_at,
                           m.source_candidate_observations.c.id)
            ).mappings()]
            candidate["review_history"] = [dict(row) for row in connection.execute(
                select(m.source_candidate_reviews).where(
                    m.source_candidate_reviews.c.candidate_id == candidate["id"],
                    m.source_candidate_reviews.c.profile_id == profile_id,
                ).order_by(m.source_candidate_reviews.c.reviewed_at,
                           m.source_candidate_reviews.c.id)
            ).mappings()]
        return {"candidates": candidates}


def report_source_candidate(store, *, profile_id: str, request_id: str,
                           discovery_origin: str | None, source_url: str | None,
                           application_url: str | None, observed_hops=None):
    """Persist a user-reported source URL even when job/application metadata is unknown."""
    store.profile(profile_id)
    capture_method = "MANUAL_CAPTURE"
    capture_key = f"{capture_method}:{request_id}"
    observation_id = identity("source-candidate-observation", profile_id, capture_key)
    with store.engine.begin() as connection:
        existed = connection.execute(select(m.source_candidate_observations.c.id).where(
            m.source_candidate_observations.c.id == observation_id
        )).first() is not None
        candidate_id = record_capture(
            connection, profile_id=profile_id, request_id=request_id,
            job_id=None, application_id=None, discovery_origin=discovery_origin,
            source_url=source_url, application_url=application_url,
            observed_hops=observed_hops, capture_method=capture_method,
        )
        if candidate_id is None:
            raise ValueError("A valid HTTP or HTTPS source URL is required")
    candidate = next(row for row in list_candidates(store, profile_id)["candidates"]
                     if row["id"] == candidate_id)
    return {"candidate": candidate, "created": not existed}


def _review_note(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("SOURCE_CANDIDATE_REVIEW_INVALID")
    normalized = value.strip()
    if not normalized or len(normalized) > 1000:
        raise ValueError("SOURCE_CANDIDATE_REVIEW_INVALID")
    return normalized


def review_candidate(store, profile_id: str, candidate_id: str,
                     state: str, site_type: str, reason: str, request_id: str,
                     api_feed_evidence_status: str, api_feed_evidence_reference: str | None,
                     api_feed_evidence_note: str,
                     access_terms_note: str, capabilities_limits_note: str,
                     expected_value_maintenance_note: str):
    store.profile(profile_id)
    reason = _review_note(reason)
    request_id = request_id.strip()
    if (state not in {"NEEDS_REVIEW", "INTEGRATION_QUEUED", "BLOCKED", "REJECTED"}
            or site_type not in {"JOB_BOARD", "EMPLOYER", "ATS", "UNKNOWN"}
            or api_feed_evidence_status not in {"PUBLIC_DOCUMENTED", "UNKNOWN", "BLOCKED"}
            or not request_id):
        raise ValueError("SOURCE_CANDIDATE_REVIEW_INVALID")
    api_feed_evidence_note = _review_note(api_feed_evidence_note)
    raw_reference = api_feed_evidence_reference
    api_feed_evidence_reference = _safe_url(raw_reference)
    if raw_reference and not api_feed_evidence_reference:
        raise ValueError("SOURCE_CANDIDATE_REVIEW_REFERENCE_INVALID")
    if api_feed_evidence_status == "PUBLIC_DOCUMENTED" and not api_feed_evidence_reference:
        raise ValueError("SOURCE_CANDIDATE_PUBLIC_EVIDENCE_REQUIRES_SAFE_REFERENCE")
    access_terms_note = _review_note(access_terms_note)
    capabilities_limits_note = _review_note(capabilities_limits_note)
    expected_value_maintenance_note = _review_note(
        expected_value_maintenance_note)
    event_id = identity("source-candidate-review", profile_id, candidate_id, request_id)
    with store.engine.begin() as connection:
        candidate = connection.execute(select(m.source_candidates).where(
            m.source_candidates.c.id == candidate_id,
            m.source_candidates.c.profile_id == profile_id,
        ).with_for_update()).mappings().one_or_none()
        if candidate is None:
            raise NoResultFound()
        previous = connection.execute(select(m.source_candidate_reviews).where(
            m.source_candidate_reviews.c.id == event_id)).mappings().one_or_none()
        if previous is not None:
            if (previous["state"] != state or previous["site_type"] != site_type
                    or previous["reason"] != reason
                    or previous["api_feed_evidence_status"] != api_feed_evidence_status
                    or previous["api_feed_evidence_reference"] != api_feed_evidence_reference
                    or previous["api_feed_evidence_note"] != api_feed_evidence_note
                    or previous["access_terms_note"] != access_terms_note
                    or previous["capabilities_limits_note"] != capabilities_limits_note
                    or previous["expected_value_maintenance_note"] != expected_value_maintenance_note):
                raise SourceCandidateConflict("Source candidate review request was reused for different content")
        else:
            timestamp = now()
            connection.execute(m.source_candidate_reviews.insert().values(
                id=event_id, candidate_id=candidate_id, profile_id=profile_id,
                state=state, site_type=site_type, reason=reason,
                api_feed_evidence_status=api_feed_evidence_status,
                api_feed_evidence_reference=api_feed_evidence_reference,
                api_feed_evidence_note=api_feed_evidence_note,
                access_terms_note=access_terms_note,
                capabilities_limits_note=capabilities_limits_note,
                expected_value_maintenance_note=expected_value_maintenance_note,
                reviewed_at=timestamp,
            ))
            connection.execute(update(m.source_candidates).where(
                m.source_candidates.c.id == candidate_id,
                m.source_candidates.c.profile_id == profile_id,
            ).values(state=state, site_type=site_type, review_reason=reason))
    candidate = next(row for row in list_candidates(store, profile_id)["candidates"]
                     if row["id"] == candidate_id)
    return candidate
