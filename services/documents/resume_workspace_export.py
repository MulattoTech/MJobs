"""Save explicitly reviewed, immutable resume workspace render snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import select

from packages.persistence.resume_workspace import resume_workspace_revisions
from services.documents.resume_templates import list_resume_templates
from services.documents.resume_workspace import (
    ResumeWorkspaceError,
    _scope_lock,
    _scope_snapshot,
    _validated_sections,
    list_resume_workspace_revisions,
)
from services.documents.resume_workspace_preview import build_resume_workspace_draft_preview
from services.documents.tailor import target_fingerprint


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _conflict(code: str, message: str):
    raise ResumeWorkspaceError(code, message)


def save_reviewed_resume_workspace_export(store, *, profile_id: str, job_id: str,
                                          expected_version: int | None,
                                          profile_version: str, job_fingerprint: str,
                                          template_id: str, sections: list[dict],
                                          preview_fingerprint: str, reviewed: bool,
                                          request_id: str) -> dict:
    """Re-render and append one reviewed revision; exact retries return the same row."""
    if reviewed is not True:
        raise ResumeWorkspaceError("RESUME_WORKSPACE_REVIEW_REQUIRED",
                                   "Review the current preview before saving.")
    if (type(request_id) is not str or not request_id.strip() or
            request_id != request_id.strip() or len(request_id) > 128):
        raise ResumeWorkspaceError("RESUME_WORKSPACE_INVALID", "A stable request identifier is required.")
    if type(preview_fingerprint) is not str or not _SHA256.fullmatch(preview_fingerprint):
        raise ResumeWorkspaceError("RESUME_WORKSPACE_PREVIEW_STALE",
                                   "Refresh and review the current workspace preview before saving.")

    catalog = {row["template_id"]: row for row in list_resume_templates()}
    template = catalog.get(template_id)
    if template is None:
        raise ResumeWorkspaceError("RESUME_WORKSPACE_INVALID", "The selected template is unavailable.")
    payload = {
        "expected_version": expected_version,
        "profile_version": profile_version,
        "job_fingerprint": job_fingerprint,
        "template_id": template_id,
        "sections": sections,
        "preview_fingerprint": preview_fingerprint,
    }
    payload_fingerprint = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    # Exact idempotent retries return their original revision even if the live
    # profile/job has moved on. Reusing an ID with changed content fails before
    # rendering the changed content.
    with store.engine.connect() as connection:
        existing = connection.execute(
            select(resume_workspace_revisions)
            .where(resume_workspace_revisions.c.profile_id == profile_id,
                   resume_workspace_revisions.c.job_id == job_id,
                   resume_workspace_revisions.c.request_id == request_id)
        ).mappings().one_or_none()
    if existing is not None:
        if existing["payload_fingerprint"] != payload_fingerprint:
            _conflict("RESUME_WORKSPACE_REQUEST_CONFLICT",
                      "This request identifier was already used for a different reviewed layout.")
        result = dict(existing)
        result["replayed"] = True
        return result

    preview = build_resume_workspace_draft_preview(
        store, profile_id, job_id,
        template_id=template_id,
        sections=sections,
        expected_profile_version=profile_version,
        expected_job_fingerprint=job_fingerprint,
        expected_template_version=template["version"],
    )
    if preview["preview_fingerprint"] != preview_fingerprint:
        raise ResumeWorkspaceError("RESUME_WORKSPACE_PREVIEW_STALE",
                                   "The reviewed preview no longer matches current approved facts or job data.")

    try:
        with store.engine.begin() as connection:
            _scope_lock(connection, profile_id, job_id)
            profile, job = _scope_snapshot(connection, store, profile_id, job_id)
            if profile.get("version") != profile_version or target_fingerprint(job) != job_fingerprint:
                _conflict("RESUME_WORKSPACE_STALE_CONTEXT",
                          "Profile or job data changed after preview; reload and preview again.")
            normalized_sections = _validated_sections(preview["sections"], profile)
            existing = connection.execute(
                select(resume_workspace_revisions)
                .where(resume_workspace_revisions.c.profile_id == profile_id,
                       resume_workspace_revisions.c.job_id == job_id,
                       resume_workspace_revisions.c.request_id == request_id)
                .with_for_update()
            ).mappings().one_or_none()
            if existing is not None:
                if existing["payload_fingerprint"] != payload_fingerprint:
                    _conflict("RESUME_WORKSPACE_REQUEST_CONFLICT",
                              "This request identifier was already used for a different reviewed layout.")
                result = dict(existing)
                result["replayed"] = True
                return result

            latest = connection.execute(
                select(resume_workspace_revisions.c.version)
                .where(resume_workspace_revisions.c.profile_id == profile_id,
                       resume_workspace_revisions.c.job_id == job_id)
                .order_by(resume_workspace_revisions.c.version.desc())
                .limit(1).with_for_update()
            ).first()
            current_version = latest[0] if latest else 0
            if current_version != (expected_version or 0):
                _conflict("RESUME_WORKSPACE_STALE_VERSION",
                          "The workspace version changed; reload before saving.")

            row_id = uuid4().hex
            connection.execute(resume_workspace_revisions.insert().values(
                id=row_id,
                profile_id=profile_id,
                job_id=job_id,
                version=current_version + 1,
                profile_version=profile_version,
                job_fingerprint=job_fingerprint,
                template_id=template_id,
                sections=normalized_sections,
                preview_fingerprint=preview_fingerprint,
                rendered_text=preview["rendered_text"],
                source_refs=preview["source_refs"],
                request_id=request_id,
                payload_fingerprint=payload_fingerprint,
                created_at=datetime.now(timezone.utc),
            ))
            saved = connection.execute(
                select(resume_workspace_revisions)
                .where(resume_workspace_revisions.c.id == row_id)
            ).mappings().one()
            result = dict(saved)
            result["replayed"] = False
            return result
    except ResumeWorkspaceError:
        raise
    except Exception as error:
        raise ResumeWorkspaceError("RESUME_WORKSPACE_INVALID",
                                   "The reviewed workspace could not be saved.") from error


def read_reviewed_resume_workspace_export(store, profile_id: str, job_id: str,
                                          version: int) -> str:
    """Return only a stored immutable text export for the exact scoped revision."""
    rows = list_resume_workspace_revisions(store, profile_id, job_id)
    row = next((item for item in rows if item.get("version") == version), None)
    if row is None or not isinstance(row.get("rendered_text"), str):
        raise ResumeWorkspaceError("RESUME_WORKSPACE_NOT_FOUND",
                                   "The reviewed text export is unavailable.")
    return row["rendered_text"]
