"""Persist immutable, profile/job-scoped resume workspace configurations."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from uuid import uuid4

from sqlalchemy import select, text

from packages.persistence import models as m
from packages.persistence.resume_workspace import resume_workspace_revisions
from services.documents.tailor import eligible_rows, target_fingerprint
from services.documents.templates import CONTRACTS, HEADINGS
from services.normalization.jobs import canonical_job_view


_SECTION_FIELDS = {"section_id", "visible", "position", "fact_ids"}
_MAX_SECTIONS = 30
_MAX_FACTS_PER_SECTION = 200


class ResumeWorkspaceError(ValueError):
    """Stable service validation error for the resume workspace API boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _invalid(message: str):
    raise ResumeWorkspaceError("RESUME_WORKSPACE_INVALID", message)


def _stale(message: str):
    raise ResumeWorkspaceError("RESUME_WORKSPACE_STALE_VERSION", message)


def _scope_snapshot(connection, store, profile_id: str, job_id: str):
    try:
        profile = store.profile(profile_id, connection=connection)
        row = connection.execute(
            select(m.jobs, m.companies.c.name.label("company"))
            .join(m.companies, m.jobs.c.company_id == m.companies.c.id)
            .where(m.jobs.c.id == job_id)
        ).mappings().one_or_none()
    except Exception as error:
        if isinstance(error, ResumeWorkspaceError):
            raise
        _invalid("The requested profile or job is unavailable")
    if row is None:
        _invalid("The requested profile or job is unavailable")
    job = canonical_job_view(dict(row))
    return profile, job


def _validate_identity(profile_id, job_id):
    if (type(profile_id) is not str or not profile_id.strip() or len(profile_id) > 128 or
            type(job_id) is not str or not job_id.strip() or len(job_id) > 128):
        _invalid("A profile and job identifier are required")


def _validated_sections(sections, profile):
    if type(sections) is not list or len(sections) > _MAX_SECTIONS:
        _invalid("Provide an ordered list of up to 30 resume sections")
    approved_ids = {row["fact_id"] for row in eligible_rows(profile)}
    normalized = []
    seen_sections = set()
    for expected_position, section in enumerate(sections):
        if type(section) is not dict or set(section) != _SECTION_FIELDS:
            _invalid("Each section must contain section_id, visible, position, and fact_ids")
        section_id = section["section_id"]
        if (type(section_id) is not str or section_id not in HEADINGS or
                section_id in seen_sections):
            _invalid("A section identifier is unsupported or duplicated")
        if type(section["visible"]) is not bool:
            _invalid("Section visibility must be an explicit boolean")
        if type(section["position"]) is not int or section["position"] != expected_position:
            _invalid("Section positions must match their explicit list order")
        fact_ids = section["fact_ids"]
        if (type(fact_ids) is not list or len(fact_ids) > _MAX_FACTS_PER_SECTION or
                any(type(fact_id) is not str or not fact_id.strip() or len(fact_id) > 128
                    for fact_id in fact_ids) or len(set(fact_ids)) != len(fact_ids)):
            _invalid("Section fact references must be unique career fact identifiers")
        if not set(fact_ids).issubset(approved_ids):
            _invalid("Workspace sections may reference only current approved profile facts")
        seen_sections.add(section_id)
        normalized.append({
            "section_id": section_id,
            "visible": section["visible"],
            "position": expected_position,
            "fact_ids": list(fact_ids),
        })
    return normalized


def _row(row):
    result = dict(row)
    result["sections"] = deepcopy(result["sections"])
    return result


def _scope_lock(connection, profile_id: str, job_id: str):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        return
    if connection.dialect.name == "postgresql":
        lock_bytes = hashlib.sha256(f"resume-workspace\0{profile_id}\0{job_id}".encode()).digest()[:8]
        lock_key = int.from_bytes(lock_bytes, "big") & 0x7FFFFFFFFFFFFFFF
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        return
    _invalid("The configured persistence backend is unsupported")


def list_resume_workspace_revisions(store, profile_id: str, job_id: str) -> list[dict]:
    """Read a scoped history in revision order without exposing another scope."""
    _validate_identity(profile_id, job_id)
    try:
        with store.engine.connect() as connection:
            _scope_snapshot(connection, store, profile_id, job_id)
            rows = connection.execute(
                select(resume_workspace_revisions)
                .where(resume_workspace_revisions.c.profile_id == profile_id,
                       resume_workspace_revisions.c.job_id == job_id)
                .order_by(resume_workspace_revisions.c.version)
            ).mappings().all()
        return [_row(item) for item in rows]
    except ResumeWorkspaceError:
        raise
    except Exception as error:
        raise ResumeWorkspaceError("RESUME_WORKSPACE_INVALID",
                                   "The requested workspace history is unavailable") from error


def save_resume_workspace_revision(store, *, profile_id: str, job_id: str,
                                   expected_version: int | None, profile_version: str,
                                   job_fingerprint: str, template_id: str,
                                   sections: list[dict]) -> dict:
    """Append a new immutable workspace revision using optimistic concurrency.

    An exact retry with the prior expected version returns the already-created row.
    No profile facts or generated documents are written by this service.
    """
    _validate_identity(profile_id, job_id)
    if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
        _invalid("Expected workspace version must be null or a positive integer")
    if (type(profile_version) is not str or not profile_version or len(profile_version) > 128 or
            type(job_fingerprint) is not str or not job_fingerprint or len(job_fingerprint) > 128):
        _invalid("Profile and job snapshot identifiers are required")
    if type(template_id) is not str or template_id not in CONTRACTS or len(template_id) > 100:
        _invalid("The selected resume template is unsupported")

    try:
        with store.engine.begin() as connection:
            _scope_lock(connection, profile_id, job_id)
            profile, job = _scope_snapshot(connection, store, profile_id, job_id)
            if profile.get("version") != profile_version:
                _stale("The profile changed after this workspace was loaded")
            if target_fingerprint(job) != job_fingerprint:
                _stale("The job changed after this workspace was loaded")
            normalized_sections = _validated_sections(sections, profile)

            latest = connection.execute(
                select(resume_workspace_revisions)
                .where(resume_workspace_revisions.c.profile_id == profile_id,
                       resume_workspace_revisions.c.job_id == job_id)
                .order_by(resume_workspace_revisions.c.version.desc())
                .limit(1)
                .with_for_update()
            ).mappings().one_or_none()
            current_version = latest["version"] if latest else 0
            expected_value = expected_version or 0
            if current_version != expected_value:
                if (latest is not None and current_version == expected_value + 1 and
                        latest["profile_version"] == profile_version and
                        latest["job_fingerprint"] == job_fingerprint and
                        latest["template_id"] == template_id and
                        latest["sections"] == normalized_sections):
                    return _row(latest)
                _stale("The workspace changed since it was loaded; reload before saving")

            values = {
                "id": uuid4().hex,
                "profile_id": profile_id,
                "job_id": job_id,
                "version": current_version + 1,
                "profile_version": profile_version,
                "job_fingerprint": job_fingerprint,
                "template_id": template_id,
                "sections": normalized_sections,
                "created_at": datetime.now(timezone.utc),
            }
            connection.execute(resume_workspace_revisions.insert().values(**values))
            inserted = connection.execute(
                select(resume_workspace_revisions).where(
                    resume_workspace_revisions.c.id == values["id"])
            ).mappings().one()
            return _row(inserted)
    except ResumeWorkspaceError:
        raise
    except Exception as error:
        raise ResumeWorkspaceError("RESUME_WORKSPACE_INVALID",
                                   "The workspace revision could not be saved") from error
