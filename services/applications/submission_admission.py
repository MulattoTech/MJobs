"""Transactional submission admission with durable live-intent evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.domain.safety import Decision, Policy, SubmissionContext, evaluate_submission
from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.applications.configuration import configured_submission_policy
from services.company_discovery.public_client import _parse_url
from services.dedupe.route_redirects import (
    ROUTE_CHECK_TIMEOUT_SECONDS, _domain, qualified_ats_tenant, resolve_aggregator_redirect,
)
from services.dedupe.routes import qualify_route_authority


GRANTED = "LIVE_SUBMISSION_INTENT_GRANTED"
CONSUMED = "LIVE_SUBMISSION_INTENT_CONSUMED"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: object) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _grant_id(receipt_id: str) -> str:
    return identity("live-submission-intent-grant", receipt_id)


def _consume_id(receipt_id: str) -> str:
    return identity("live-submission-intent-consume", receipt_id)


def _receipt_id(value: object) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 128):
        raise ValueError("LIVE_SUBMISSION_INTENT_INVALID")
    return value


def grant_live_submission_intent(
        store, *, receipt_id: str, profile_id: str, application_id: str,
        job_id: str, granted_at: datetime, expires_at: datetime,
        source_allowed: bool) -> dict:
    """Persist immutable user intent; no public caller is provided here."""
    receipt_id = _receipt_id(receipt_id)
    if (not all(isinstance(value, str) and value for value in (
            profile_id, application_id, job_id))
            or not _aware(granted_at) or not _aware(expires_at)
            or granted_at >= expires_at or type(source_allowed) is not bool):
        raise ValueError("LIVE_SUBMISSION_INTENT_INVALID")
    payload = {
        "receipt_id": receipt_id,
        "profile_id": profile_id,
        "application_id": application_id,
        "job_id": job_id,
        "granted_at": granted_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "source_allowed": source_allowed,
    }
    with store.engine.begin() as connection:
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == application_id).with_for_update()).mappings().one()
        if (application["profile_id"] != profile_id or application["job_id"] != job_id
                or application["target"] != "REAL"):
            raise ValueError("LIVE_SUBMISSION_INTENT_SCOPE_MISMATCH")
        connection.execute(m.audit.insert().values(
            id=_grant_id(receipt_id), entity_id=application_id,
            action=GRANTED, payload=payload, timestamp=granted_at.isoformat()))
    return payload


def _load_intent(connection, receipt_id: object, application: dict,
                 evaluated_at: datetime) -> tuple[dict | None, str | None]:
    try:
        normalized_id = _receipt_id(receipt_id)
    except ValueError:
        return None, "DURABLE_JOB_INTENT_REQUIRED"
    row = connection.execute(select(m.audit).where(
        m.audit.c.id == _grant_id(normalized_id),
        m.audit.c.action == GRANTED,
    ).with_for_update()).mappings().one_or_none()
    if row is None:
        return None, "DURABLE_JOB_INTENT_REQUIRED"
    consumed = connection.execute(select(m.audit.c.id).where(
        m.audit.c.id == _consume_id(normalized_id),
        m.audit.c.entity_id == application["id"],
        m.audit.c.action == CONSUMED,
    )).first()
    if consumed is not None:
        return None, "JOB_INTENT_ALREADY_CONSUMED"
    payload = row["payload"]
    if not isinstance(payload, dict):
        return None, "INVALID_JOB_INTENT"
    try:
        granted_at = datetime.fromisoformat(payload["granted_at"])
        expires_at = datetime.fromisoformat(payload["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None, "INVALID_JOB_INTENT"
    expected = {
        "receipt_id": normalized_id,
        "profile_id": application["profile_id"],
        "application_id": application["id"],
        "job_id": application["job_id"],
    }
    if (row["entity_id"] != application["id"]
            or any(payload.get(key) != value for key, value in expected.items())
            or type(payload.get("source_allowed")) is not bool
            or not _aware(granted_at) or not _aware(expires_at)):
        return None, "JOB_INTENT_MISMATCH"
    if not granted_at <= evaluated_at < expires_at:
        return None, "STALE_JOB_INTENT"
    return {
        **expected,
        "granted_at": granted_at,
        "expires_at": expires_at,
        "source_allowed": payload["source_allowed"],
    }, None


def _consume_intent(connection, intent: dict, application: dict,
                    consumed_at: datetime) -> bool:
    values = {
        "id": _consume_id(intent["receipt_id"]),
        "entity_id": application["id"],
        "action": CONSUMED,
        "payload": {
            "receipt_id": intent["receipt_id"],
            "profile_id": application["profile_id"],
            "application_id": application["id"],
            "job_id": application["job_id"],
            "consumed_at": consumed_at.isoformat(),
        },
        "timestamp": consumed_at.isoformat(),
    }
    if connection.dialect.name == "sqlite":
        statement = sqlite_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    elif connection.dialect.name == "postgresql":
        statement = postgres_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id]).returning(m.audit.c.id)
        # psycopg may report an unknown rowcount for a successful INSERT. The
        # returned row is the atomic source of truth, including a lost race.
        return connection.execute(statement).scalar_one_or_none() == values["id"]
    else:
        raise ValueError("Unsupported database backend")
    return connection.execute(statement).rowcount == 1


def _latest_audit(connection, application_id: str, action: str) -> dict | None:
    row = connection.execute(select(m.audit).where(
        m.audit.c.entity_id == application_id,
        m.audit.c.action == action,
    ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)
        .with_for_update()).mappings().one_or_none()
    return dict(row) if row is not None else None


def _validated_materials(connection, application: dict) -> tuple[bool, bool, bool]:
    documents = [dict(row) for row in connection.execute(select(m.documents).where(
        m.documents.c.application_id == application["id"],
        m.documents.c.version == application["document_version"],
        m.documents.c.profile_version == application["profile_version"],
    ).with_for_update()).mappings()]
    validation = _latest_audit(
        connection, application["id"], "DOCUMENTS_VALIDATED")
    payload = validation["payload"] if validation is not None else None
    evidence = payload.get("documents") if isinstance(payload, dict) else None
    by_id = ({row.get("id"): row for row in evidence}
             if isinstance(evidence, list)
             and all(isinstance(row, dict) for row in evidence) else {})
    quality_approved = bool(
        documents
        and isinstance(payload, dict)
        and payload.get("profile_version") == application["profile_version"]
        and isinstance(evidence, list)
        and len(by_id) == len(evidence) == len(documents)
        and all(
            document["id"] in by_id
            and by_id[document["id"]].get("fact_ids") == document["fact_ids"]
            and by_id[document["id"]].get("sha256") == document["sha256"]
            and by_id[document["id"]].get("claim_validation")
            == (document.get("generation_context") or {}).get("claim_validation")
            for document in documents
        )
    )

    selected = application.get("fact_ids")
    fact_rows = []
    if (isinstance(selected, list) and selected
            and all(isinstance(fact_id, str) and fact_id for fact_id in selected)
            and len(set(selected)) == len(selected)):
        fact_rows = [dict(row) for row in connection.execute(select(m.facts).where(
            m.facts.c.profile_id == application["profile_id"],
            m.facts.c.version == application["profile_version"],
            m.facts.c.fact_id.in_(selected),
        )).mappings()]
    facts_verified = bool(
        quality_approved
        and len(fact_rows) == len(selected or ())
        and {row["fact_id"] for row in fact_rows} == set(selected or ())
        and all(
            row["verified"] is True
            and row["user_approved"] is True
            and isinstance(row["source"], str) and bool(row["source"].strip())
            and isinstance(row["text"], str) and bool(row["text"].strip())
            and type(row["confidence"]) in (int, float)
            and row["confidence"] >= 80
            for row in fact_rows
        )
    )

    preflight = _latest_audit(
        connection, application["id"], "APPLICATION_QUESTION_PREFLIGHT")
    preflight_payload = preflight["payload"] if preflight is not None else None
    answer_review = (preflight_payload.get("answer_review", [])
                     if isinstance(preflight_payload, dict) else None)
    sensitive_answers_approved = bool(
        isinstance(preflight_payload, dict)
        and preflight_payload.get("result") == "PASS"
        and preflight_payload.get("profile_version") == application["profile_version"]
        and preflight_payload.get("document_version") == application["document_version"]
        and preflight_payload.get("route_id") == application["route_id"]
        and preflight_payload.get("missing") == []
        and isinstance(answer_review, list)
        and all(isinstance(row, dict) and row.get("review_required") is False
                for row in answer_review)
    )
    return quality_approved, facts_verified, sensitive_answers_approved


def _context(application: dict, match: dict, confidence: float, hourly: int,
             daily: int, company: int, evaluated_at: datetime,
             intent: dict | None, barrier: str | None,
             materials: tuple[bool, bool, bool]) -> SubmissionContext:
    is_live = application["target"] == "REAL"
    quality_approved, facts_verified, sensitive_answers_approved = materials
    return SubmissionContext(
        target="EXTERNAL" if is_live else "MOCK",
        state=application["state"],
        user_enabled=bool(intent) if is_live else False,
        actual_user_intended_job=bool(intent) if is_live else False,
        job_id=application["job_id"] if is_live else "",
        intent_receipt_id=intent["receipt_id"] if intent else "",
        intent_job_id=intent["job_id"] if intent else "",
        intent_granted_at=intent["granted_at"] if intent else None,
        intent_expires_at=intent["expires_at"] if intent else None,
        evaluated_at=evaluated_at if intent else None,
        match_score=match["score"], confidence=confidence,
        quality_approved=quality_approved, facts_verified=facts_verified,
        source_allowed=(intent["source_allowed"] if intent else not is_live),
        sensitive_answers_approved=sensitive_answers_approved,
        hourly_count=hourly, daily_count=daily, company_count=company,
        barrier=barrier,
    )



ROUTE_CHECKED = "APPLICATION_SUBMISSION_ROUTE_CHECKED"
_ROUTE_CHECK_REASONS = frozenset({
    "CLIENT_CLOSED", "CONTENT_ENCODING_UNSUPPORTED", "DNS_BUSY", "DNS_EMPTY",
    "DNS_INVALID", "DNS_NOT_GLOBAL", "DNS_PRIVATE", "FETCH_DEADLINE",
    "NETWORK_FAILED", "HTTP_STATUS_BLOCKED", "HTTP_STATUS_ERROR",
    "REDIRECT_BLOCKED", "REDIRECT_LIMIT",
    "REDIRECT_LOCATION_MISSING", "REQUEST_BUDGET", "RESPONSE_HEADERS_TOO_LARGE",
    "RESPONSE_TOO_LARGE", "ROBOTS_DENIED", "ROBOTS_UNAVAILABLE", "URL_NOT_ALLOWED",
})


@dataclass(frozen=True)
class _RouteCheck:
    """Private one-call snapshot; never accepted from an API or saved as authority."""
    input_sha256: str
    route: dict
    domains: tuple[str, ...]
    tenants: tuple[dict, ...]
    result: dict | None = None
    completed_monotonic: float | None = None


def _route_check_snapshot(connection, application, job, profile, intent):
    route = connection.execute(select(m.routes).where(
        m.routes.c.id == application["route_id"],
        m.routes.c.job_id == application["job_id"],
        m.routes.c.company_id == job["company_id"],
    ).with_for_update()).mappings().one_or_none()
    if route is None:
        return Decision(False, ("APPLICATION_ROUTE_CHANGED",))
    route = dict(route)
    # Bound identity input cardinality. Do not silently ignore excess evidence.
    domains = [dict(row) for row in connection.execute(select(m.company_domains).where(
        m.company_domains.c.company_id == route["company_id"],
        m.company_domains.c.verified.is_(True),
    ).order_by(m.company_domains.c.id).limit(101).with_for_update()).mappings()]
    tenants = [dict(row) for row in connection.execute(select(m.company_ats_tenants).where(
        m.company_ats_tenants.c.company_id == route["company_id"],
    ).order_by(m.company_ats_tenants.c.id).limit(101).with_for_update()).mappings()]
    if len(domains) > 100 or len(tenants) > 100:
        return Decision(False, ("APPLICATION_ROUTE_EVIDENCE_UNAVAILABLE",))
    parsed = _parse_url(route["url"])
    if parsed is None:
        return Decision(False, ("APPLICATION_ROUTE_DISCOVERY_BLOCKED",))
    normalized_domains = tuple(domain for domain in (
        _domain(row["domain"]) for row in domains) if domain)
    # Board/provider labels rank provenance; only the actual public URL and
    # current company evidence can exempt a destination from the fresh probe.
    if (any(parsed[1] == domain or parsed[1].endswith("." + domain)
            for domain in normalized_domains)
            or qualified_ats_tenant(route["url"], tenants) is not None):
        return None
    authority = qualify_route_authority(connection, [route])[0]["authority"]
    documents = [dict(row) for row in connection.execute(select(
        m.documents.c.id, m.documents.c.kind, m.documents.c.profile_version,
        m.documents.c.version, m.documents.c.fact_ids, m.documents.c.sha256,
        m.documents.c.template, m.documents.c.generation_model,
    ).where(
        m.documents.c.application_id == application["id"],
        m.documents.c.version == application["document_version"],
    ).order_by(m.documents.c.id).with_for_update()).mappings()]
    binding = {
        "application": {key: application[key] for key in (
            "id", "job_id", "route_id", "profile_id", "target", "profile_version",
            "document_version", "fact_ids")},
        "current_profile_version": profile["version"],
        "route": route, "route_authority": authority,
        "domains": domains, "tenants": tenants,
        "documents": documents,
        "intent": {**intent, "granted_at": intent["granted_at"].isoformat(),
                   "expires_at": intent["expires_at"].isoformat()},
    }
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True).encode()).hexdigest()
    return _RouteCheck(digest, route, normalized_domains, tuple(tenants))


def _route_check_result(result, snapshot):
    """Keep a verified destination private; audit only fixed codes and hashes."""
    if not isinstance(result, dict):
        return {"status": "INVALID", "reason": "INVALID_RESULT"}
    status = result.get("status")
    if not isinstance(status, str):
        return {"status": "INVALID", "reason": "INVALID_RESULT"}
    if status == "UNRESOLVED":
        return {"status": "UNRESOLVED"}
    if status == "BLOCKED":
        reason = result.get("reason")
        return {"status": "BLOCKED", "reason": (
            reason if isinstance(reason, str) and reason in _ROUTE_CHECK_REASONS
            else "PROBE_UNAVAILABLE")}
    if status not in {"RESOLVED_EMPLOYER_ROUTE", "RESOLVED_ATS_ROUTE"}:
        return {"status": "INVALID", "reason": "INVALID_RESULT"}
    candidate = result.get("candidate_url")
    if not isinstance(candidate, str) or not candidate or len(candidate) > 2000:
        return {"status": "INVALID", "reason": "INVALID_RESULT"}
    try:
        parsed = urlsplit(candidate)
        if (_parse_url(candidate) is None or parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.port not in (None, 443)):
            raise ValueError()
        if status == "RESOLVED_ATS_ROUTE":
            tenant = qualified_ats_tenant(candidate, snapshot.tenants)
            valid = bool(tenant and tenant == {
                "provider": result.get("provider"), "tenant": result.get("tenant")})
        else:
            host = parsed.hostname.casefold().rstrip(".")
            valid = any(host == domain.casefold().rstrip(".")
                        or host.endswith("." + domain.casefold().rstrip("."))
                        for domain in snapshot.domains)
        if not valid:
            raise ValueError()
    except (ValueError, TypeError):
        return {"status": "INVALID", "reason": "DESTINATION_NOT_VERIFIED"}
    return {"status": status, "candidate_url_sha256": hashlib.sha256(candidate.encode()).hexdigest()}


def _record_route_check(store, connection, application_id, probe, *, status=None, reason=None):
    payload = {
        "schema_version": 1, "source_route_id": probe.route["id"],
        "input_sha256": probe.input_sha256,
        **(probe.result or {"status": "INVALID", "reason": "INVALID_RESULT"}),
    }
    if status is not None:
        payload["status"] = status
    if reason is not None:
        payload["reason"] = reason
    store._audit(connection, application_id, ROUTE_CHECKED, payload)


def authorize_submission(
        store, application_id: str, confidence: float, policy: Policy | None = None,
        *, intent_receipt_id: str | None = None, barrier: str | None = None,
        check=None) -> Decision:
    """Check a REAL route outside locks, then atomically revalidate and claim.

    Disabled or otherwise inadmissible requests never fetch. The deployment's
    external gate remains OFF. A redirect candidate never changes the selected
    route. Failed probes require review; a completed unresolved attempt may
    continue only when every existing submission gate still permits it.
    """
    first = _authorize_transaction(store, application_id, confidence, policy,
        intent_receipt_id=intent_receipt_id, barrier=barrier, check=check)
    if isinstance(first, Decision):
        return first
    if check:
        check()
    result = resolve_aggregator_redirect(first.route["url"], verified_domains=first.domains,
                                         ats_tenants=first.tenants, check=check)
    # Cancellation is never converted into an unresolved or failed-but-admitted probe.
    if check:
        check()
    probe = replace(first, result=_route_check_result(result, first),
                    completed_monotonic=time.monotonic())
    return _authorize_transaction(store, application_id, confidence, policy,
        intent_receipt_id=intent_receipt_id, barrier=barrier, check=check, probe=probe)


def _authorize_transaction(
        store, application_id, confidence, policy, *, intent_receipt_id,
        barrier, check, probe=None):
    if policy is None:
        policy = configured_submission_policy()
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        application = dict(connection.execute(select(m.applications).where(
            m.applications.c.id == application_id).with_for_update()).mappings().one())
        if application["target"] not in ("MOCK", "REAL"):
            return Decision(False, ("INVALID_SUBMISSION_TARGET",))
        materials = ((True, True, True) if application["target"] == "MOCK"
                     else _validated_materials(connection, application))
        profile = connection.execute(select(m.profiles).where(
            m.profiles.c.id == application["profile_id"]).with_for_update()).mappings().one()
        if check:
            check()
        job = dict(connection.execute(select(m.jobs).where(
            m.jobs.c.id == application["job_id"]).with_for_update()).mappings().one())
        timestamp = _utc_now()
        from services.discovery.lifecycle import CLOSED_STATES
        if job["attributes"].get("status") in CLOSED_STATES:
            return Decision(False, ("JOB_CLOSED",))
        match = dict(connection.execute(select(m.matches).where(
            m.matches.c.job_id == job["id"],
            m.matches.c.profile_id == application["profile_id"],
            m.matches.c.profile_version == application["profile_version"],
        )).mappings().one())
        submitted = select(func.count()).select_from(
            m.applications.join(m.jobs, m.jobs.c.id == m.applications.c.job_id)
        ).where(
            m.applications.c.profile_id == application["profile_id"],
            m.applications.c.state.in_([
                "SUBMITTING", "CONFIRMED", "SUBMISSION_UNCONFIRMED"]),
        )
        hourly = connection.execute(submitted.where(
            m.applications.c.submitted_at >= (
                timestamp - timedelta(hours=1)).isoformat())).scalar_one()
        daily = connection.execute(submitted.where(
            m.applications.c.submitted_at >= (
                timestamp - timedelta(days=1)).isoformat())).scalar_one()
        company = connection.execute(submitted.where(
            m.jobs.c.company_id == job["company_id"])).scalar_one()
        intent = None
        intent_error = None
        if application["target"] == "REAL":
            intent, intent_error = _load_intent(
                connection, intent_receipt_id, application, timestamp)
        decision = evaluate_submission(_context(
            application, match, confidence, hourly, daily, company,
            timestamp, intent, barrier, materials), policy)
        if intent_error and intent_error not in decision.reasons:
            decision = Decision(False, (*decision.reasons, intent_error))
        if not decision.allowed:
            return decision
        if application["target"] == "REAL":
            current = _route_check_snapshot(connection, application, job, profile, intent)
            if isinstance(current, Decision):
                return current
            if probe is None and current is not None:
                return current
            if probe is not None:
                if current is None or current.input_sha256 != probe.input_sha256:
                    _record_route_check(store, connection, application_id, probe,
                                        status="STALE", reason="APPLICATION_ROUTE_CHANGED")
                    return Decision(False, ("APPLICATION_ROUTE_CHANGED",))
                if (probe.completed_monotonic is None
                        or time.monotonic() - probe.completed_monotonic > ROUTE_CHECK_TIMEOUT_SECONDS):
                    _record_route_check(store, connection, application_id, probe,
                                        status="STALE", reason="ROUTE_CHECK_EXPIRED")
                    return Decision(False, ("APPLICATION_ROUTE_CHECK_EXPIRED",))
                _record_route_check(store, connection, application_id, probe)
                if probe.result["status"] not in {
                        "UNRESOLVED", "RESOLVED_EMPLOYER_ROUTE", "RESOLVED_ATS_ROUTE"}:
                    return Decision(False, ("APPLICATION_ROUTE_DISCOVERY_BLOCKED",))
        elif probe is not None:
            return Decision(False, ("APPLICATION_ROUTE_CHANGED",))
        if check:
            check()
        # Route/evidence locks and final cancellation checks may outlive the
        # timestamp used for initial eligibility. Consent must still be valid
        # at consumption, including routes that needed no network probe.
        timestamp = _utc_now()
        if intent is not None and not intent["granted_at"] <= timestamp < intent["expires_at"]:
            return Decision(False, ("STALE_JOB_INTENT",))
        if intent is not None and not _consume_intent(
                connection, intent, application, timestamp):
            return Decision(False, ("JOB_INTENT_ALREADY_CONSUMED",))
        changed = connection.execute(update(m.applications).where(
            m.applications.c.id == application_id,
            m.applications.c.state == "READY_TO_SUBMIT",
        ).values(state="SUBMITTING", submitted_at=now(),
                 trace=[*application["trace"], "SUBMIT"]))
        if changed.rowcount != 1:
            return Decision(False, ("SUBMISSION_ALREADY_CLAIMED",))
        store._audit(connection, application_id, "SUBMITTING", {
            "provider": "mock-ats" if application["target"] == "MOCK"
            else "live-admission",
        })
        return decision
