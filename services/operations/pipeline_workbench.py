"""Read-only, profile-scoped projection of persisted job readiness."""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata

from sqlalchemy import case, func, select

from packages.persistence import models as m
from packages.persistence.store import identity
from services.dedupe.routes import load_job_routes, plan_application_routes
from services.tracking.applications import PIPELINE_STATUSES


STAGES = (
    "DISCOVERED", "NEEDS_SCORING", "NEEDS_REVIEW", "READY_TO_PREPARE",
    "PREPARING", "PREPARED", "TRACKING",
)
MAX_LIMIT = 200
_PREPARABLE_STATES = {None, "PREPARED", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED"}
_ACTIVE_STATES = {"QUEUED", "RUNNING"}
_SAFE_MESSAGES = {
    "SCORE_MISSING": "This job needs a current profile score.",
    "SCORE_STALE": "The profile changed since this job was scored.",
    "SCORE_INVALID": "The stored profile score is not usable.",
    "DOCUMENTS_MISSING": "Resume and cover letter have not been prepared.",
    "DOCUMENTS_STALE": "Prepared documents use an older profile version.",
    "DOCUMENTS_INCOMPLETE": "Resume and cover letter are not both available.",
    "FACTS_UNVERIFIED": "At least one verified, approved profile fact is required.",
    "ROUTE_UNAVAILABLE": "No usable application route is available.",
    "ROUTE_REVIEW_REQUIRED": "Choose the application route that matches this posting.",
    "ROUTE_SELECTION_REQUIRED": "Choose an application route before preparing.",
    "MOCK_ROUTE_IN_PERSONAL_MODE": "Fixture routes are unavailable in personal mode.",
    "APPLICATION_STATE_LOCKED": "The existing application state needs review.",
    "PREPARATION_FAILED": "The last preparation attempt failed and needs review.",
    "DISCOVERY_RUNNING": "A current profile scoring operation is still running.",
    "WORKSPACE_MODE_UNAVAILABLE": "Bind this workspace to an operation mode before preparing.",
}


def _safe_text(value, limit=180):
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = " ".join("".join(ch for ch in text if unicodedata.category(ch)[0] != "C").split())
    return text[:limit]


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _block(code):
    return {"code": code, "message": _SAFE_MESSAGES[code]}


def _latest(rows):
    return max(rows, key=lambda row: (str(row.get("updated_at") or ""), str(row["id"])),
               default=None)


def _doc_state(application, documents, profile_version):
    if application is None:
        return "MISSING"
    required = {"resume", "cover-letter"}
    app_docs = [doc for doc in documents if doc["application_id"] == application["id"]]
    if not app_docs:
        return "MISSING"
    latest = {}
    for doc in sorted(app_docs, key=lambda row: (row["version"], str(row["id"]))):
        latest[doc["kind"]] = doc
    if not required.issubset(latest):
        return "INCOMPLETE"
    if (application["profile_version"] != profile_version
            or any(latest[kind]["profile_version"] != profile_version
                   for kind in required)):
        return "STALE"
    if application["document_version"] is not None and any(
            latest[kind]["version"] != application["document_version"] for kind in required):
        return "INCOMPLETE"
    return "CURRENT"


def _route_plan(store, job_id, profile, application, mode):
    routes = load_job_routes(store, job_id, profile["id"])
    preference = profile.get("preferences", {}).get(
        "application_route_preference", "PREFER_VERIFIED")
    attrs = store.job(job_id).get("attributes") or {}
    resolution = attrs.get("source_resolution") if isinstance(attrs, dict) else None
    review_required = isinstance(resolution, dict) and bool(
        resolution.get("route_review_required"))
    if review_required:
        preserved = resolution.get("preserved_route_id")
        if not application or application.get("route_id") != preserved:
            return None, None, "ROUTE_REVIEW_REQUIRED"
        route_mode, selected_id = "THIS_ROUTE", preserved
    elif application and application.get("route_id"):
        route_mode, selected_id = "THIS_ROUTE", application["route_id"]
    else:
        route_mode, selected_id = "DEFAULT", None
    try:
        plan = plan_application_routes(routes, mode=route_mode, route_id=selected_id,
                                       preference=preference)
    except ValueError as error:
        code = str(error)
        if code == "APPLICATION_ROUTE_SELECTION_REQUIRED":
            return None, None, "ROUTE_SELECTION_REQUIRED"
        return None, None, "ROUTE_UNAVAILABLE"
    route = plan["selected"][0]["preferred_route"]
    if mode == "PERSONAL" and str(route.get("provider") or "").casefold() == "mock-ats":
        return None, route, "MOCK_ROUTE_IN_PERSONAL_MODE"
    return route, route, None


def read_pipeline_workbench(store, profile_id: str, limit: int = 100) -> dict:
    """Return a bounded readiness view based only on durable local records."""
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("PIPELINE_LIMIT_INVALID")
    profile = store.profile(profile_id)
    profile_version = profile["version"]
    facts = profile.get("facts") or []
    approved_fact_ids = sorted(
        str(fact["id"]) for fact in facts
        if fact.get("verified") is True and fact.get("user_approved") is True
    )
    with store.engine.connect() as connection:
        profile_job_ids = select(m.matches.c.job_id.label("job_id")).where(
            m.matches.c.profile_id == profile_id
        ).union(
            select(m.applications.c.job_id.label("job_id")).where(
                m.applications.c.profile_id == profile_id)
        ).subquery()
        total = connection.execute(select(func.count()).select_from(profile_job_ids)).scalar_one()
        selected_jobs = list(connection.execute(
            select(m.jobs, m.companies.c.name.label("company_name"))
            .select_from(
                profile_job_ids.join(m.jobs, m.jobs.c.id == profile_job_ids.c.job_id)
                .join(m.companies, m.jobs.c.company_id == m.companies.c.id))
            .order_by(func.lower(m.jobs.c.title), m.jobs.c.id)
            .limit(limit)
        ).mappings())
        selected_ids = [row["id"] for row in selected_jobs]
        ranked_matches = select(
            *m.matches.c,
            func.row_number().over(
                partition_by=m.matches.c.job_id,
                order_by=(
                    case((m.matches.c.profile_version == profile_version, 0), else_=1),
                    m.matches.c.profile_version.desc(), m.matches.c.id.desc(),
                ),
            ).label("_match_rank"),
        ).where(
            m.matches.c.profile_id == profile_id,
            m.matches.c.job_id.in_(selected_ids) if selected_ids else m.matches.c.job_id == "__none__",
        ).subquery()
        match_rows = list(connection.execute(select(
            *(ranked_matches.c[column.name] for column in m.matches.c)
        ).where(ranked_matches.c._match_rank == 1)).mappings())
        ranked_apps = select(
            *m.applications.c,
            func.row_number().over(
                partition_by=m.applications.c.job_id,
                order_by=(m.applications.c.created_at.desc(), m.applications.c.id.desc()),
            ).label("_application_rank"),
        ).where(
            m.applications.c.profile_id == profile_id,
            m.applications.c.job_id.in_(selected_ids) if selected_ids else m.applications.c.job_id == "__none__",
        ).subquery()
        apps = list(connection.execute(select(
            *(ranked_apps.c[column.name] for column in m.applications.c)
        ).where(ranked_apps.c._application_rank == 1)).mappings())
        app_by_job = {row["job_id"]: row for row in apps}
        app_ids = [row["id"] for row in apps]
        ranked_docs = select(
            *m.documents.c,
            func.row_number().over(
                partition_by=(m.documents.c.application_id, m.documents.c.kind),
                order_by=(m.documents.c.version.desc(), m.documents.c.id.desc()),
            ).label("_document_rank"),
        ).where(
            m.documents.c.application_id.in_(app_ids) if app_ids else m.documents.c.application_id == "__none__"
        ).subquery()
        docs = list(connection.execute(select(
            *(ranked_docs.c[column.name] for column in m.documents.c)
        ).where(ranked_docs.c._document_rank == 1)).mappings())
        operations = list(connection.execute(select(m.operations).where(
            m.operations.c.entity_id == profile_id,
            m.operations.c.kind.in_(["PREPARE", "DISCOVER"]),
        )).mappings())
        workspace = connection.execute(select(m.workspace_identity.c.operation_mode).where(
            m.workspace_identity.c.id == "workspace")).scalar_one_or_none()
        tracking_entities = [identity("application-tracking", profile_id, app_id)
                            for app_id in app_ids]
        audit_rows = list(connection.execute(select(m.audit).where(
            m.audit.c.action == "APPLICATION_PIPELINE_STATUS",
            m.audit.c.entity_id.in_(tracking_entities) if tracking_entities
            else m.audit.c.entity_id == "__none__",
        )).mappings()) if app_ids else []

    mode = str(workspace or "UNBOUND").upper()
    match_by_job = {row["job_id"]: row for row in match_rows}
    tracking_by_entity = {}
    for row in audit_rows:
        entity = row["entity_id"]
        payload = row.get("payload") or {}
        status = payload.get("status")
        attribution = payload.get("attribution")
        if (status in PIPELINE_STATUSES
                and attribution in {"USER_REPORTED", "EXTERNAL_USER_REPORTED",
                                    "USER_REPORTED_EMAIL"}):
            previous = tracking_by_entity.get(entity)
            sequence = payload.get("sequence", 0)
            if previous is None or sequence >= previous[0]:
                tracking_by_entity[entity] = (sequence, status, attribution)

    profile_rescores = [dict(op) for op in operations if op["kind"] == "DISCOVER"
                        and (op["params"] or {}).get("rescore_only")
                        and not (op["params"] or {}).get("job_id")]
    active_rescore = _latest([op for op in profile_rescores if op["state"] in _ACTIVE_STATES])
    scoring_operation = active_rescore or _latest(profile_rescores)
    items = []
    for job in selected_jobs:
        job_id = str(job["id"])
        app = app_by_job.get(job_id)
        score_row = match_by_job.get(job_id)
        score_state = "MISSING"
        score = None
        if score_row:
            if score_row["profile_version"] != profile_version:
                score_state = "STALE"
            elif isinstance(score_row["score"], (int, float)) and math.isfinite(score_row["score"]):
                score_state, score = "CURRENT", score_row["score"]
            else:
                score_state = "INVALID"
        documents_state = _doc_state(app, docs, profile_version)
        job_operations = [dict(op) for op in operations
                          if (op["params"] or {}).get("job_id") == job_id]
        active = _latest([op for op in job_operations if op["state"] in _ACTIVE_STATES])
        prepare_op = _latest([op for op in job_operations if op["kind"] == "PREPARE"])
        discovery_active = bool(active_rescore) or any(
            op["kind"] == "DISCOVER" and op["state"] in _ACTIVE_STATES
            and (op["params"] or {}).get("rescore_only") for op in job_operations)
        route, route_for_hash, route_blocker = _route_plan(store, job_id, profile, app, mode)
        blockers = []
        if score_state != "CURRENT":
            blockers.append(_block({"MISSING": "SCORE_MISSING", "STALE": "SCORE_STALE",
                                    "INVALID": "SCORE_INVALID"}[score_state]))
        if documents_state != "CURRENT":
            blockers.append(_block({"MISSING": "DOCUMENTS_MISSING", "STALE": "DOCUMENTS_STALE",
                                    "INCOMPLETE": "DOCUMENTS_INCOMPLETE"}[documents_state]))
        if not approved_fact_ids:
            blockers.append(_block("FACTS_UNVERIFIED"))
        if route_blocker:
            blockers.append(_block(route_blocker))
        if mode not in {"FIXTURE", "PERSONAL"}:
            blockers.append(_block("WORKSPACE_MODE_UNAVAILABLE"))
        if discovery_active:
            blockers.append(_block("DISCOVERY_RUNNING"))
        if prepare_op and prepare_op["state"] in {"FAILED", "BLOCKED", "DEAD_LETTERED"}:
            blockers.append(_block("PREPARATION_FAILED"))
        if app and app["state"] not in _PREPARABLE_STATES:
            blockers.append(_block("APPLICATION_STATE_LOCKED"))

        entity = identity("application-tracking", profile_id, app["id"]) if app else None
        tracking = tracking_by_entity.get(entity) if entity else None
        can_prepare = bool(
            score_state == "CURRENT" and approved_fact_ids and route and not route_blocker
            and mode in {"FIXTURE", "PERSONAL"}
            and not active and not discovery_active and not tracking
            and (not app or app["state"] in _PREPARABLE_STATES)
            and not (prepare_op and prepare_op["state"] in {"FAILED", "BLOCKED", "DEAD_LETTERED"})
        )
        if tracking:
            stage, next_action = "TRACKING", "OPEN_APPLICATION"
        elif active and active["kind"] == "PREPARE":
            stage, next_action = "PREPARING", "WAIT_FOR_OPERATION"
        elif score_state != "CURRENT":
            stage, next_action = "NEEDS_SCORING", "REVIEW_MATCH"
        elif documents_state == "CURRENT":
            stage, next_action = "PREPARED", "OPEN_APPLICATION"
        elif can_prepare:
            stage, next_action = "READY_TO_PREPARE", "PREPARE"
        else:
            stage, next_action = "NEEDS_REVIEW", "REVIEW_ROUTE" if route_blocker else "REVIEW_MATCH"
        route_id = str(route.get("id")) if route else None
        operation = active or prepare_op
        fingerprint_input = {
            "profile_id": profile_id, "profile_version": profile_version,
            "approved_fact_ids": approved_fact_ids, "mode": mode, "job": dict(job),
            "score": dict(score_row) if score_row else None,
            "application": dict(app) if app else None,
            "documents": [{key: doc[key] for key in ("kind", "version", "profile_version",
                                                       "template", "sha256")}
                          for doc in docs if app and doc["application_id"] == app["id"]],
            "route": dict(route_for_hash) if route_for_hash else None,
            "operation": dict(operation) if operation else None,
            "active_rescore": active_rescore["id"] if active_rescore else None,
            "tracking": tracking,
        }
        items.append({
            "job_id": job_id, "title": _safe_text(job["title"]),
            "company": _safe_text(job["company_name"]), "score": score,
            "application_id": app["id"] if app else None, "stage": stage,
            "blockers": blockers, "can_prepare": can_prepare,
            "fingerprint": _fingerprint(fingerprint_input), "route_id": route_id,
            "score_state": score_state, "documents_state": documents_state,
            "tracking_status": tracking[1] if tracking else None,
            "tracking_attribution": tracking[2] if tracking else None,
            "operation_id": operation["id"] if operation else None,
            "operation_kind": operation["kind"] if operation else None,
            "operation_state": operation["state"] if operation else None,
            "operation_mode": mode, "route_mode": "THIS_ROUTE" if app and route else "DEFAULT",
            "next_action": next_action,
        })
    by_stage = {stage: sum(item["stage"] == stage for item in items) for stage in STAGES}
    return {"profile_id": profile_id, "profile_version": profile_version, "items": items,
            "scoring_operation": {"operation_id": scoring_operation["id"], "state": scoring_operation["state"]}
            if scoring_operation else None,
            "counts": {"total": len(items), "by_stage": by_stage},
            "truncated": total > len(items)}
