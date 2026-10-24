"""Durable trigger policy for company recruiting-infrastructure discovery."""
from __future__ import annotations

import json
from sqlalchemy import select, text

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.company_discovery.flywheel import directly_monitored
from services.company_discovery.domains import resolve_verified_company_domain
from services.company_discovery.resolution import candidate_ats_provider, resolve_company
from services.company_discovery.public_resolution import resolve_public_company
from workers.dispatch import enqueue_operation


TRIGGERS = {
    "PREVIOUSLY_UNSEEN_COMPANY",
    "HIGH_MATCH_JOB",
    "USER_SAVED_COMPANY",
    "USER_PREFERRED_COMPANY",
    "USER_ADDED_COMPANY",
    "NEW_ATS_TENANT",
    "REPEATED_AGGREGATOR_APPEARANCE",
    "KNOWN_SOURCE_BROKEN",
}


def queue_company_discovery(store, candidate, reason, profile_id=None, *, public=False, request_id=None):
    reason = str(reason or "").upper()
    if reason not in TRIGGERS:
        raise ValueError("COMPANY_DISCOVERY_TRIGGER_INVALID")
    if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
        raise ValueError("COMPANY_DISCOVERY_REQUEST_ID_INVALID")
    candidate = dict(candidate)
    fingerprint = _intent_key(candidate, reason, profile_id, public)
    if public and not (candidate.get("company_website") or candidate.get("website_url")):
        existing = resolve_company(store, candidate)
        if not existing["is_new"]:
            official_domain = resolve_verified_company_domain(store, existing["company_id"])
            if official_domain["status"] == "VERIFIED":
                candidate["company_website"] = f"https://{official_domain['domain']}"
    if public and reason == "USER_ADDED_COMPANY":
        return _queue_public_add(store, candidate, profile_id, request_id, fingerprint)
    resolved = resolve_public_company(store, candidate) if public else resolve_company(store, candidate)
    if directly_monitored(store, resolved["company_id"], public=public) and reason not in {"KNOWN_SOURCE_BROKEN", "NEW_ATS_TENANT"}:
        return {
            "status": "ALREADY_MONITORED",
            "company_id": resolved["company_id"],
            "operation_id": None,
            "reason": reason,
        }
    params = _params(candidate, reason, profile_id, public, resolved, request_id)
    operation_id = enqueue_operation(store, "COMPANY_DISCOVER", resolved["company_id"], params)
    return _queued(resolved["company_id"], operation_id, reason)


def _params(candidate, reason, profile_id, public, resolved, request_id):
    params = {
        "candidate": {**candidate, "trigger_evidence": [reason]},
        "fetch_mode": "PUBLIC_HTTPS" if public else "FIXTURE",
        "trigger_reason": reason,
        "profile_id": profile_id,
        "resolution": {
            "company_id": resolved["company_id"],
            "confidence": resolved["confidence"],
            "evidence": resolved["evidence"],
        },
    }
    if request_id is not None:
        params["request_id"] = request_id
    return params


def _queued(company_id, operation_id, reason):
    return {
        "status": "QUEUED",
        "company_id": company_id,
        "operation_id": operation_id,
        "reason": reason,
    }


def _lock_intent(connection, key):
    if connection.dialect.name == 'postgresql':
        lock_id = int.from_bytes(bytes.fromhex(key)[:8], 'big', signed=True)
        connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_id})


def _intent_key(*parts):
    return identity(json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(',', ':')))


def _queue_public_add(store, candidate, profile_id, request_id, fingerprint):
    """Coalesce active equivalent work and retain each explicit intent's receipt.

    Review and monitoring operations carry separate authority and never participate.
    Receipts use the existing audit ledger; worker input snapshots stay immutable.
    """
    reason = 'USER_ADDED_COMPANY'
    receipt_id = identity('company-discovery-intent-v1', profile_id, request_id)
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        if request_id is not None:
            _lock_intent(connection, receipt_id)
            receipt = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.id == receipt_id)).scalar_one_or_none()
            if receipt is not None:
                if receipt.get('fingerprint') != fingerprint:
                    raise ValueError('COMPANY_DISCOVERY_REQUEST_CONFLICT')
                return receipt['response']
        # Serialize before reading current profile/domain evidence and the outbox.
        _lock_intent(connection, _intent_key('company-discovery-add-lock-v1', profile_id, candidate))
        resolved = resolve_public_company(store, candidate, connection=connection, lock_verified=True)
        version = store.profile(profile_id, connection=connection)['version'] if profile_id else None
        active_key = _intent_key('company-discovery-active-v1', resolved['company_id'],
                                candidate, profile_id, version)
        active = connection.execute(select(m.operations).where(
            m.operations.c.kind == 'COMPANY_DISCOVER',
            m.operations.c.entity_id == resolved['company_id'],
            m.operations.c.state.in_(['QUEUED', 'RUNNING']),
            m.operations.c.params['active_intent_key'].as_string() == active_key,
            m.operations.c.params['trigger_reason'].as_string() == reason,
            m.operations.c.params['discovery_request_id'].as_string().is_(None),
            m.operations.c.params['monitoring_source_id'].as_string().is_(None),
        ).order_by(m.operations.c.updated_at, m.operations.c.id).limit(1)).mappings().one_or_none()
        if active is not None:
            response = _queued(active['entity_id'], active['id'], reason)
        elif directly_monitored(store, resolved['company_id'], public=True, connection=connection):
            response = dict(status='ALREADY_MONITORED', company_id=resolved['company_id'],
                            operation_id=None, reason=reason)
        else:
            params = _params(candidate, reason, profile_id, True, resolved, request_id)
            params['profile_version'] = version
            params['active_intent_key'] = active_key
            operation_id = enqueue_operation(store, 'COMPANY_DISCOVER', resolved['company_id'],
                                             params, connection=connection)
            response = _queued(resolved['company_id'], operation_id, reason)
        if request_id is not None:
            connection.execute(m.audit.insert().values(id=receipt_id,
                entity_id=response['company_id'], action='COMPANY_DISCOVERY_INTENT',
                payload={'fingerprint': fingerprint, 'profile_id': profile_id,
                         'request_id': request_id, 'response': response}, timestamp=now()))
        return response


def triggers_for_job(store, job, match_score=None):
    attributes = job.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    candidate = {
        "company_name": job.get("company"),
        "company_website": attributes.get("company_website"),
        "application_url": job.get("application_url"),
        "ats_tenant": attributes.get("tenant"),
    }
    # Only propagate normalized job evidence, never a top-level-only label.
    declaration = {"ats_provider": attributes.get("provider")}
    if "provider" in job:
        declaration["provider"] = job["provider"]
    provider = candidate_ats_provider(declaration)
    if provider is not None:
        candidate["ats_provider"] = provider
    resolved = resolve_company(store, candidate)
    reasons = []
    if resolved["is_new"] or not directly_monitored(store, resolved["company_id"]):
        reasons.append("PREVIOUSLY_UNSEEN_COMPANY")
    if type(match_score) in (int, float) and match_score >= 80:
        reasons.append("HIGH_MATCH_JOB")
    return candidate, reasons
