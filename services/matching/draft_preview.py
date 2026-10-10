"""Bounded, no-write comparison of saved and draft scoring configurations.

This is a diagnostic sample of current-profile stored matches, not a submission
shortlist. It does not schedule work, audit, persist scores or call a provider.
"""
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.discovery.matching import build_explanation
from services.discovery.search_semantics import evaluate_search
from services.matching.selected_search_scoring import compose_selected_search_match

MAX_CANDIDATES = 100
MAX_DIMENSIONS = 32


class DraftPreviewError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str, allow_nan=False).encode()).hexdigest()


def preview_draft(store, profile_id, search_id, draft, expected_version):
    """Read one consistent snapshot; all ranking and score work remains in memory."""
    if (len(set(draft["weights"]) | set(draft["thresholds"])) > MAX_DIMENSIONS
            or not any(weight > 0 for weight in draft["weights"].values())):
        raise DraftPreviewError("SCORING_PREVIEW_DRAFT_INVALID", 422)
    with store.engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN")
        elif connection.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        try:
            profile = store.profile(profile_id, connection=connection)
        except NoResultFound:
            raise DraftPreviewError("SCORING_PREVIEW_SCOPE_NOT_FOUND", 404) from None
        search = connection.execute(select(m.search_profiles).where(
            m.search_profiles.c.id == search_id, m.search_profiles.c.profile_id == profile_id,
        )).mappings().one_or_none()
        if search is None:
            raise DraftPreviewError("SCORING_PREVIEW_SCOPE_NOT_FOUND", 404)
        criteria = search["criteria"]
        if not isinstance(criteria, dict):
            raise DraftPreviewError("SCORING_PREVIEW_CONTEXT_INVALID")
        current = criteria.get("role_scoring_profile")
        if current is not None and not isinstance(current, dict):
            raise DraftPreviewError("SCORING_PREVIEW_CONTEXT_INVALID")
        current_version = current.get("version") if current else None
        if expected_version != current_version:
            raise DraftPreviewError("SCORING_PREVIEW_VERSION_CONFLICT")
        # These workspace-only filters depend on application pipeline state and
        # cannot be approximated by semantic matching. Refuse rather than omit.
        job_filters = criteria.get("job_filters") or {}
        if not isinstance(job_filters, dict) or any(value not in (None, "", [], False)
                                                    for value in job_filters.values()):
            raise DraftPreviewError("SCORING_PREVIEW_FILTERS_UNSUPPORTED")
        statement = select(m.jobs, m.companies.c.name.label("company")).join(
            m.matches, (m.matches.c.job_id == m.jobs.c.id)
            & (m.matches.c.profile_id == profile_id)
            & (m.matches.c.profile_version == profile["version"]),
        ).join(m.companies, m.companies.c.id == m.jobs.c.company_id)
        sources = search["source_ids"] or []
        if not isinstance(sources, list) or any(not isinstance(source, str) for source in sources):
            raise DraftPreviewError("SCORING_PREVIEW_CONTEXT_INVALID")
        if sources:
            statement = statement.where(select(m.postings.c.id).where(
                m.postings.c.job_id == m.jobs.c.id, m.postings.c.source.in_(sources),
            ).exists())
        candidates = [dict(row) for row in connection.execute(
            statement.order_by(m.jobs.c.id).limit(MAX_CANDIDATES + 1),
        ).mappings()]
    truncated = len(candidates) > MAX_CANDIDATES
    candidates = candidates[:MAX_CANDIDATES]
    draft_id = _fingerprint(draft)
    draft_criteria = {**criteria, "role_scoring_profile": {
        "id": "unsaved-preview", "version": draft_id, **draft,
    }}
    at = datetime.now(timezone.utc)
    rows, skipped = [], 0
    for job in candidates:
        try:
            if not evaluate_search(criteria, job, at=at)["matched"]:
                continue
            baseline = build_explanation(job, profile)
            current_score = compose_selected_search_match(job, baseline, criteria, candidate_profile=profile)
            draft_score = compose_selected_search_match(job, baseline, draft_criteria, candidate_profile=profile)
            rows.append({
                "job_id": job["id"], "title": job["title"] or "Untitled stored job",
                "current_score": current_score["score"], "draft_score": draft_score["score"],
                "delta": round(draft_score["score"] - current_score["score"], 2),
                "draft_contributions": draft_score["scoring_contributions"],
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            # Do not invent a score when a stored optional record is malformed.
            skipped += 1
    for rank, row in enumerate(sorted(rows, key=lambda item: (-item["current_score"], item["job_id"])), 1):
        row["current_rank"] = rank
    rows.sort(key=lambda item: (-item["draft_score"], item["job_id"]))
    for rank, row in enumerate(rows, 1):
        row["draft_rank"] = rank
    return {
        "schema_version": 1, "read_only": True, "profile_id": profile_id,
        "search_profile_id": search_id, "profile_version": profile["version"],
        "current_version": current_version, "draft_id": draft_id,
        "snapshot_id": _fingerprint({"profile_version": profile["version"], "criteria": criteria,
                                      "draft_id": draft_id, "results": rows, "truncated": truncated}),
        "candidate_limit": MAX_CANDIDATES, "scanned_count": len(candidates),
        "eligible_count": len(rows), "skipped_count": skipped, "truncated": truncated,
        "jobs": rows,
        "limitation": (
            "Ranks cover only this bounded sample of current-profile stored matches after saved-search "
            "semantic/source filters, not all jobs or the application queue. Hidden/blacklisted jobs can "
            "appear in this diagnostic sample. Current scores are recomputed, not stored score readback. "
            "Nothing is saved or submitted; unknown evidence remains neutral."
        ),
    }
