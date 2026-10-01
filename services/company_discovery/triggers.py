"""Durable trigger policy for company recruiting-infrastructure discovery."""
from __future__ import annotations

from sqlalchemy import select

from packages.persistence import models as m
from services.company_discovery.flywheel import directly_monitored
from services.company_discovery.resolution import resolve_company
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


def queue_company_discovery(store, candidate, reason, profile_id=None):
    reason = str(reason or "").upper()
    if reason not in TRIGGERS:
        raise ValueError("COMPANY_DISCOVERY_TRIGGER_INVALID")
    resolved = resolve_company(store, candidate)
    if directly_monitored(store, resolved["company_id"]) and reason not in {"KNOWN_SOURCE_BROKEN", "NEW_ATS_TENANT"}:
        return {
            "status": "ALREADY_MONITORED",
            "company_id": resolved["company_id"],
            "operation_id": None,
            "reason": reason,
        }
    candidate = {**candidate, "trigger_evidence": [reason]}
    params = {
        "candidate": candidate,
        "trigger_reason": reason,
        "profile_id": profile_id,
        "resolution": {
            "company_id": resolved["company_id"],
            "confidence": resolved["confidence"],
            "evidence": resolved["evidence"],
        },
    }
    operation_id = enqueue_operation(store, "COMPANY_DISCOVER", resolved["company_id"], params)
    return {
        "status": "QUEUED",
        "company_id": resolved["company_id"],
        "operation_id": operation_id,
        "reason": reason,
    }


def triggers_for_job(store, job, match_score=None):
    candidate = {
        "company_name": job.get("company"),
        "company_website": (job.get("attributes") or {}).get("company_website"),
        "application_url": job.get("application_url"),
        "ats_tenant": (job.get("attributes") or {}).get("tenant"),
    }
    resolved = resolve_company(store, candidate)
    reasons = []
    if resolved["is_new"] or not directly_monitored(store, resolved["company_id"]):
        reasons.append("PREVIOUSLY_UNSEEN_COMPANY")
    if type(match_score) in (int, float) and match_score >= 80:
        reasons.append("HIGH_MATCH_JOB")
    return candidate, reasons
