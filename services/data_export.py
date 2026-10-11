"""Deterministic profile-scoped CSV, JSON, and safe backup exports."""
from __future__ import annotations

import csv
from datetime import date, datetime
import io
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import case, func, select, union
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence import resume_workspace as _resume_workspace  # registers its table


MAX_EXPORT_ROWS = 10_000
MAX_EXPORT_BYTES = 5 * 1024 * 1024

_JOB_COLUMNS = (
    "id", "company", "requisition", "title", "description", "skills", "location",
    "remote_type", "application_url", "match_score", "application_state",
)
_APPLICATION_COLUMNS = (
    "id", "job_id", "title", "company", "state", "target", "provider",
    "application_url", "created_at", "submitted_at",
)
_EXCLUDED_KEY_PARTS = (
    "password", "credential", "secret", "token", "cookie", "browser", "session",
    "raw", "diagnostic", "trace", "confirmation", "steps", "generationcontext",
)
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


class DataExportError(ValueError):
    """A bounded, profile-scoped export cannot be produced safely."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _profile(connection, profile_id):
    if not isinstance(profile_id, str) or not profile_id or len(profile_id) > 128:
        raise DataExportError("PROFILE_NOT_FOUND")
    row = connection.execute(select(
        m.profiles.c.id, m.profiles.c.name, m.profiles.c.email, m.profiles.c.version,
    ).where(m.profiles.c.id == profile_id)).mappings().one_or_none()
    if row is None:
        raise DataExportError("PROFILE_NOT_FOUND")
    return dict(row)


def _safe_json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            key_text = str(key)
            compact_key = _NON_ALPHANUMERIC.sub("", key_text.casefold())
            if any(part in compact_key for part in _EXCLUDED_KEY_PARTS):
                continue
            safe[key_text] = _safe_json_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item) for item in value]
    # Binary blobs and arbitrary objects are never serialized into user exports.
    return None


def _json_text(value) -> str:
    try:
        encoded = json.dumps(
            _safe_json_value(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError):
        raise DataExportError("EXPORT_INVALID_DATA") from None
    if len(encoded.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise DataExportError("EXPORT_TOO_LARGE")
    return encoded


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.casefold() not in {"http", "https"}
                or not parsed.hostname or parsed.username is not None or parsed.password is not None):
            return None
        # Query strings and fragments can carry access tokens or session state.
        host = parsed.hostname.casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme.casefold(), host, parsed.path, "", ""))
    except ValueError:
        return None


def _bounded_rows(connection, table, profile_id, columns, *, order_by=None):
    selected = [table.c[name] for name in columns]
    statement = select(*selected).where(table.c.profile_id == profile_id)
    statement = statement.order_by(*(order_by or (table.c.id,))).limit(MAX_EXPORT_ROWS + 1)
    return _collect_rows(connection, statement)


def _collect_rows(connection, statement):
    rows = []
    total_bytes = 2
    try:
        for row in connection.execute(statement).mappings():
            if len(rows) >= MAX_EXPORT_ROWS:
                raise DataExportError("EXPORT_TOO_LARGE")
            safe = _safe_json_value(dict(row))
            encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
            total_bytes += len(encoded.encode("utf-8")) + 1
            if total_bytes > MAX_EXPORT_BYTES:
                raise DataExportError("EXPORT_TOO_LARGE")
            rows.append(safe)
    except (RecursionError, TypeError, ValueError) as error:
        if isinstance(error, DataExportError):
            raise
        raise DataExportError("EXPORT_INVALID_DATA") from None
    return rows


def _profile_job_ids(profile_id):
    return union(
        select(m.matches.c.job_id).where(m.matches.c.profile_id == profile_id),
        select(m.applications.c.job_id).where(m.applications.c.profile_id == profile_id),
    ).subquery("profile_export_job_ids")


def _jobs(connection, profile, profile_id):
    profile_job_ids = _profile_job_ids(profile_id)
    route_choice = select(
        m.routes.c.job_id, func.min(m.routes.c.id).label("route_id"),
    ).group_by(m.routes.c.job_id).subquery("profile_export_routes")
    route_id = case(
        (m.applications.c.route_id.is_not(None), m.applications.c.route_id),
        else_=route_choice.c.route_id,
    )
    statement = select(
        m.jobs.c.id, m.companies.c.name.label("company"), m.jobs.c.requisition,
        m.jobs.c.title, m.jobs.c.description, m.jobs.c.skills, m.jobs.c.location,
        m.jobs.c.remote_type, m.routes.c.url.label("application_url"),
        m.matches.c.score.label("match_score"),
        m.applications.c.state.label("application_state"),
    ).select_from(
        m.jobs.join(profile_job_ids, profile_job_ids.c.job_id == m.jobs.c.id)
        .join(m.companies, m.companies.c.id == m.jobs.c.company_id)
        .outerjoin(m.matches, (m.matches.c.job_id == m.jobs.c.id)
                   & (m.matches.c.profile_id == profile_id)
                   & (m.matches.c.profile_version == profile["version"]))
        .outerjoin(m.applications, (m.applications.c.job_id == m.jobs.c.id)
                   & (m.applications.c.profile_id == profile_id))
        .outerjoin(route_choice, route_choice.c.job_id == m.jobs.c.id)
        .outerjoin(m.routes, m.routes.c.id == route_id)
    ).order_by(m.jobs.c.title, m.companies.c.name, m.jobs.c.id).limit(MAX_EXPORT_ROWS + 1)
    rows = _collect_rows(connection, statement)
    for row in rows:
        row["application_url"] = _safe_url(row.get("application_url"))
        row["skills"] = _safe_json_value(row.get("skills"))
    return rows


def _applications(connection, profile_id):
    statement = select(
        m.applications.c.id, m.applications.c.job_id, m.jobs.c.title,
        m.companies.c.name.label("company"), m.applications.c.state,
        m.applications.c.target, m.routes.c.provider,
        m.routes.c.url.label("application_url"), m.applications.c.created_at,
        m.applications.c.submitted_at,
    ).select_from(
        m.applications.join(m.jobs, m.jobs.c.id == m.applications.c.job_id)
        .join(m.companies, m.companies.c.id == m.jobs.c.company_id)
        .join(m.routes, m.routes.c.id == m.applications.c.route_id)
    ).where(m.applications.c.profile_id == profile_id).order_by(
        m.applications.c.created_at, m.applications.c.id,
    ).limit(MAX_EXPORT_ROWS + 1)
    rows = _collect_rows(connection, statement)
    for row in rows:
        row["application_url"] = _safe_url(row.get("application_url"))
    return rows


def _csv_cell(value):
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(_safe_json_value(value), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    elif value is None:
        value = ""
    elif isinstance(value, (date, datetime)):
        value = value.isoformat()
    elif not isinstance(value, str):
        value = str(value)
    # Prevent spreadsheet formula execution while preserving the source text.
    if value.lstrip().startswith(("=", "+", "-", "@")):
        value = "'" + value
    return value


def _csv(rows, columns):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(_csv_cell(row.get(column)) for column in columns)
    result = output.getvalue()
    if len(result.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise DataExportError("EXPORT_TOO_LARGE")
    return result


def export_jobs_csv(store, profile_id: str) -> str:
    """Export matched or applied jobs for exactly one existing profile."""
    with store.engine.connect() as connection:
        profile = _profile(connection, profile_id)
        return _csv(_jobs(connection, profile, profile_id), _JOB_COLUMNS)


def export_applications_csv(store, profile_id: str) -> str:
    """Export safe application status fields for exactly one existing profile."""
    with store.engine.connect() as connection:
        _profile(connection, profile_id)
        return _csv(_applications(connection, profile_id), _APPLICATION_COLUMNS)


def _profile_rows(connection, table, profile_id, columns):
    return _bounded_rows(connection, table, profile_id, columns)


def export_profile_backup(store, profile_id: str) -> dict:
    """Build a complete bounded JSON-compatible backup of safe profile records.

    Credential ciphertext, provider/session state, raw posting diagnostics,
    application traces/confirmations, attempt steps, and private document paths
    are intentionally excluded. Uploaded document bytes live outside this
    relational service and are represented only by safe metadata.
    """
    with store.engine.connect() as connection:
        profile = _profile(connection, profile_id)
        jobs = _jobs(connection, profile, profile_id)
        applications = _applications(connection, profile_id)
        profile_job_ids = _profile_job_ids(profile_id)
        route_ids = select(m.routes.c.id).join(
            profile_job_ids, profile_job_ids.c.job_id == m.routes.c.job_id,
        ).subquery("profile_export_route_ids")

        route_statement = (select(
            m.routes.c.id, m.routes.c.job_id, m.routes.c.company_id, m.routes.c.provider,
            m.routes.c.tenant, m.routes.c.requisition, m.routes.c.url,
        ).where(m.routes.c.id.in_(select(route_ids.c.id))).order_by(m.routes.c.id)
        .limit(MAX_EXPORT_ROWS + 1))
        route_rows = _collect_rows(connection, route_statement)
        for row in route_rows:
            row["url"] = _safe_url(row["url"])

        application_ids = select(m.applications.c.id).where(
            m.applications.c.profile_id == profile_id,
        ).subquery("profile_export_application_ids")
        related_specs = {
            "application_route_choices": (
                m.application_route_choices,
                ("id", "application_id", "route_id", "requested_mode", "state", "created_at"),
                m.application_route_choices.c.application_id.in_(select(application_ids.c.id)),
            ),
            "documents": (
                m.documents,
                ("id", "application_id", "kind", "version", "profile_version", "fact_ids",
                 "sha256", "template", "created_at", "generation_model"),
                m.documents.c.application_id.in_(select(application_ids.c.id)),
            ),
            "attempts": (
                m.attempts,
                ("id", "application_id", "state", "started_at"),
                m.attempts.c.application_id.in_(select(application_ids.c.id)),
            ),
        }
        related = {}
        for name, (table, columns, predicate) in related_specs.items():
            statement = select(*(table.c[column] for column in columns)).where(
                predicate).order_by(table.c.id).limit(MAX_EXPORT_ROWS + 1)
            related[name] = _collect_rows(connection, statement)

        snapshot = {
            "schema_version": 1,
            "export_type": "profile_backup",
            "profile_id": profile_id,
            "profile": profile,
            "profile_versions": _profile_rows(connection, m.profile_versions, profile_id,
                                               ("id", "version", "contact", "answers")),
            "facts": _profile_rows(connection, m.facts, profile_id,
                                   ("id", "version", "fact_id", "kind", "text", "source",
                                    "verified", "user_approved", "confidence")),
            "settings": {
                "ai_task_settings": _profile_rows(connection, m.ai_task_settings, profile_id,
                                                   ("id", "task", "provider", "model", "updated_at")),
                "ai_cost_policy": _profile_rows(connection, m.ai_cost_policies, profile_id,
                                                 ("id", "minimum_score", "max_input_candidates",
                                                  "max_ai_candidates", "max_document_candidates", "updated_at")),
                "answer_confidence_policy": _profile_rows(
                    connection, m.answer_confidence_policies, profile_id,
                    ("id", "minimum_saved_answer_confidence", "minimum_question_match_confidence",
                     "updated_at"),
                ),
                "search_profiles": _profile_rows(connection, m.search_profiles, profile_id,
                                                   ("id", "name", "enabled", "criteria", "source_ids",
                                                    "created_at", "updated_at")),
            },
            "career_stories": _profile_rows(
                connection, m.career_stories, profile_id,
                ("id", "story_key", "revision", "profile_version", "kind", "title", "text",
                 "fact_ids", "fact_fingerprints", "tags", "allowed_uses", "review_status",
                 "review_note", "created_at", "reviewed_at"),
            ),
            "ai_evaluation_runs": _profile_rows(
                connection, m.ai_evaluation_runs, profile_id,
                ("id", "profile_version", "policy", "task_provider", "task_model",
                 "evaluated_candidate_count", "created_at"),
            ),
            "ai_job_evaluations": _profile_rows(
                connection, m.ai_job_evaluations, profile_id,
                ("id", "run_id", "job_id", "title", "company", "deterministic_score",
                 "category", "rationale", "created_at"),
            ),
            "ai_document_reviews": _profile_rows(
                connection, m.ai_document_reviews, profile_id,
                ("id", "run_id", "job_id", "reviewed_at"),
            ),
            "source_candidates": _profile_rows(
                connection, m.source_candidates, profile_id,
                ("id", "domain", "display_name", "site_type", "state", "first_seen_at",
                 "last_seen_at", "collection_enabled", "review_reason", "integration_issue"),
            ),
            "source_candidate_reviews": _profile_rows(
                connection, m.source_candidate_reviews, profile_id,
                ("id", "candidate_id", "state", "site_type", "reason", "api_feed_evidence_status",
                 "api_feed_evidence_reference", "api_feed_evidence_note", "access_terms_note",
                 "capabilities_limits_note", "expected_value_maintenance_note", "integration_issue",
                 "reviewed_at"),
            ),
            "company_discovery_requests": _profile_rows(
                connection, m.company_discovery_requests, profile_id,
                ("id", "company_id", "trigger_job_id", "profile_version", "reasons",
                 "highest_match_score", "created_at", "last_seen_at", "state", "revision",
                 "selected_website"),
            ),
            "jobs": jobs,
            "job_matches": _profile_rows(
                connection, m.matches, profile_id,
                ("id", "job_id", "profile_version", "score", "missing", "components"),
            ),
            "applications": applications,
            "application_routes": [_safe_json_value(row) for row in route_rows],
            **related,
            "resume_revisions": _profile_rows(
                connection, m.resume_revisions, profile_id,
                ("id", "upload_id", "series_id", "label", "version", "created_at", "document_kind"),
            ),
            "uploaded_resumes": _profile_rows(
                connection, m.uploaded_resumes, profile_id,
                ("id", "filename", "format", "media_type", "size_bytes", "sha256", "status",
                 "created_at"),
            ),
            "resume_workspace_revisions": _profile_rows(
                connection, _resume_workspace.resume_workspace_revisions, profile_id,
                ("id", "job_id", "version", "profile_version", "job_fingerprint", "template_id",
                 "sections", "preview_fingerprint", "rendered_text", "source_refs", "created_at"),
            ),
            "interview_prep_packs": _profile_rows(
                connection, m.interview_prep_packs, profile_id,
                ("id", "job_id", "pack_version", "profile_version", "job_fingerprint",
                 "selected_story_revision_ids", "questions", "talking_points", "star_prompts",
                 "missing_evidence", "export_text", "created_at"),
            ),
        }
        _json_text(snapshot)  # enforce the serialized size limit before returning
        return _safe_json_value(snapshot)


def export_profile_json(store, profile_id: str) -> str:
    """Return a deterministic JSON export containing the full safe backup."""
    snapshot = export_profile_backup(store, profile_id)
    snapshot["export_type"] = "profile_json_export"
    return _json_text(snapshot)
