"""Transactional persistence for explicit fresh-profile recovery."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from sqlalchemy import select, text

from packages.domain.profile import ContactDetails, JobPreferences
from packages.domain.safety import Fact
from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.documents.uploads import validate_resume


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProfileRestoreError(ValueError):
    """Base class for a safe, sanitized profile restore failure."""

    code = "PROFILE_RESTORE_FAILED"


class ProfileRestoreConflict(ProfileRestoreError):
    code = "PROFILE_RESTORE_CONFLICT"


class ProfileRestoreFileError(ProfileRestoreError):
    code = "PROFILE_RESTORE_FILE_WRITE_FAILED"


class ProfileRestoreInvalidPlan(ProfileRestoreError):
    code = "PROFILE_RESTORE_PLAN_INVALID"


def _validate_plan(plan: Any, request_id: str) -> tuple[dict, str, list[dict], list[dict]]:
    if not isinstance(plan, dict) or not isinstance(plan.get("profile"), dict):
        raise ProfileRestoreInvalidPlan("Restore plan is invalid")
    fingerprint = plan.get("fingerprint")
    if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
        raise ProfileRestoreInvalidPlan("Restore plan is invalid")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise ProfileRestoreInvalidPlan("Restore request is invalid")

    source = plan["profile"]
    profile_id = source.get("id")
    if not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 128:
        raise ProfileRestoreInvalidPlan("Restore target is invalid")
    if not all(isinstance(source.get(field), str) and source[field].strip()
               for field in ("name", "email")):
        raise ProfileRestoreInvalidPlan("Restore profile is invalid")
    if len(source["name"]) > 200 or len(source["email"]) > 320:
        raise ProfileRestoreInvalidPlan("Restore profile is invalid")
    try:
        contact = ContactDetails.model_validate(source.get("contact", {})).model_dump()
        preferences = JobPreferences.model_validate(source.get("preferences", {})).model_dump()
    except Exception:
        raise ProfileRestoreInvalidPlan("Restore profile is invalid") from None
    preferences["company_discovery"]["company_discovery_enabled"] = False
    preferences["application_route_preference"] = "USER_CHOICE"
    sensitive_categories = (
        "RACE_ETHNICITY", "RELIGION", "SEX_GENDER", "DISABILITY",
        "VETERAN_STATUS", "OTHER_VOLUNTARY_DEMOGRAPHIC",
    )
    preferences["sensitive_answer_policies"] = {
        category: {"mode": "ALWAYS_REQUIRE_REVIEW", "response": None}
        for category in sensitive_categories
    }

    facts = source.get("facts")
    answers = source.get("answers")
    uploads = plan.get("uploaded_files", [])
    search_profiles = plan.get("search_profiles", [])
    if (not isinstance(facts, list) or len(facts) > 200
            or not isinstance(answers, list) or len(answers) > 200
            or not isinstance(uploads, list) or len(uploads) > 200
            or not isinstance(search_profiles, list) or len(search_profiles) > 100):
        raise ProfileRestoreInvalidPlan("Restore collections are invalid")

    normalized_facts = []
    normalized_answers = []
    for collection, output, is_answer in (
        (facts, normalized_facts, False), (answers, normalized_answers, True),
    ):
        seen_ids: set[str] = set()
        for row in collection:
            if not isinstance(row, dict):
                raise ProfileRestoreInvalidPlan("Restore entry is invalid")
            identifier = row.get("id")
            if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128:
                raise ProfileRestoreInvalidPlan("Restore entry is invalid")
            if identifier in seen_ids:
                raise ProfileRestoreInvalidPlan("Restore entry identity is duplicated")
            seen_ids.add(identifier)
            try:
                normalized = {
                    key: row[key] for key in (
                        "id", "text", "source", "verified", "user_approved", "confidence",
                    )
                }
                validated = Fact(**normalized)
            except Exception:
                raise ProfileRestoreInvalidPlan("Restore entry is invalid") from None
            item = {
                **validated.__dict__,
                "verified": False,
                "user_approved": False,
                "confidence": 0.0,
            }
            if not is_answer:
                kind = row.get("kind", "skill")
                if not isinstance(kind, str) or not kind.strip() or len(kind) > 32:
                    raise ProfileRestoreInvalidPlan("Restore fact is invalid")
                item["kind"] = kind
            else:
                question = row.get("question")
                if question is not None:
                    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
                        raise ProfileRestoreInvalidPlan("Restore answer is invalid")
                    item["question"] = question
            output.append(item)

    normalized_uploads = []
    upload_sources: set[str] = set()
    upload_digests: set[str] = set()
    total_bytes = 0
    for item in uploads:
        if not isinstance(item, dict):
            raise ProfileRestoreInvalidPlan("Restore file is invalid")
        source_id, filename = item.get("source_id"), item.get("filename")
        file_format, media_type = item.get("format"), item.get("media_type")
        content, digest = item.get("content"), item.get("sha256")
        size = item.get("size_bytes")
        if (not isinstance(source_id, str) or not source_id or len(source_id) > 128
                or source_id in upload_sources or not isinstance(content, bytes)
                or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                or not isinstance(size, int) or isinstance(size, bool) or size != len(content)
                or hashlib.sha256(content).hexdigest() != digest
                or not isinstance(filename, str) or not isinstance(file_format, str)
                or not isinstance(media_type, str)):
            raise ProfileRestoreInvalidPlan("Restore file is invalid")
        upload_sources.add(source_id)
        if digest in upload_digests:
            raise ProfileRestoreInvalidPlan("Restore file is duplicated")
        upload_digests.add(digest)
        total_bytes += size
        if total_bytes > 5 * 1024 * 1024:
            raise ProfileRestoreInvalidPlan("Restore files exceed the safe size limit")
        try:
            validated_format = validate_resume(filename, media_type, content)
        except Exception:
            raise ProfileRestoreInvalidPlan("Restore file is invalid") from None
        if file_format.casefold() != validated_format:
            raise ProfileRestoreInvalidPlan("Restore file format is invalid")
        normalized_uploads.append({**item, "format": validated_format})

    normalized_search_profiles = []
    seen_names: set[str] = set()
    for item in search_profiles:
        if not isinstance(item, dict):
            raise ProfileRestoreInvalidPlan("Restore search profile is invalid")
        name, criteria = item.get("name"), item.get("criteria")
        if (not isinstance(name, str) or not name.strip() or len(name) > 200
                or name in seen_names or not isinstance(criteria, dict)):
            raise ProfileRestoreInvalidPlan("Restore search profile is invalid")
        seen_names.add(name)
        normalized_search_profiles.append({
            "name": name,
            "criteria": criteria,
        })

    normalized_profile = {
        "id": profile_id,
        "name": source["name"],
        "email": source["email"],
        "contact": contact,
        "preferences": preferences,
        "facts": normalized_facts,
        "answers": normalized_answers,
    }
    return normalized_profile, fingerprint, normalized_uploads, normalized_search_profiles


def _receipt_id(request_id: str) -> str:
    return identity("profile-restore-request", request_id)


def _read_receipt(connection, request_id: str, fingerprint: str):
    row = connection.execute(select(m.audit).where(
        m.audit.c.id == _receipt_id(request_id),
    )).mappings().one_or_none()
    if row is None:
        return None
    payload = row["payload"]
    if (not isinstance(payload, dict) or payload.get("request_id") != request_id
            or not isinstance(payload.get("profile_id"), str)):
        raise ProfileRestoreConflict("Restore request identity conflicts with a prior receipt")
    if payload.get("fingerprint") != fingerprint:
        raise ProfileRestoreConflict("Restore request was already used with different content")
    return {"profile_id": payload["profile_id"], "request_id": request_id, "replayed": True}


def _lock_request(connection, request_id: str) -> None:
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif connection.dialect.name == "postgresql":
        lock_digest = hashlib.sha256(request_id.encode("utf-8")).digest()[:8]
        lock_id = int.from_bytes(lock_digest, "big", signed=False)
        if lock_id >= 2**63:
            lock_id -= 2**64
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
    else:
        raise ProfileRestoreError("Unsupported database backend")


def _uploads_directory(output_dir: str | Path) -> Path:
    try:
        configured = Path(output_dir)
        if configured.is_symlink():
            raise OSError
        root = configured.resolve(strict=True)
        if not root.is_dir():
            raise OSError
        uploads = root / "uploads"
        uploads.mkdir(mode=0o700, exist_ok=True)
        if uploads.is_symlink() or uploads.resolve().parent != root:
            raise OSError
        return uploads
    except (OSError, RuntimeError, ValueError):
        raise ProfileRestoreFileError("Restore file storage is unavailable") from None


def _stage_files(uploads: list[dict], output_dir: str | Path):
    if not uploads:
        return None, []
    directory = _uploads_directory(output_dir)
    staged = []
    try:
        for item in uploads:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".restore-", delete=False) as handle:
                staged_path = Path(handle.name)
                staged.append((item, staged_path))
                handle.write(item["content"])
                handle.flush()
                os.fsync(handle.fileno())
        return directory, staged
    except OSError:
        for _, path in staged:
            path.unlink(missing_ok=True)
        raise ProfileRestoreFileError("Restore file could not be staged") from None


def _profile_rows(profile: dict, fingerprint: str):
    version = identity("restored-profile-version", profile, fingerprint)
    snapshot = {
        "id": identity(profile["id"], version),
        "profile_id": profile["id"],
        "version": version,
        "contact": {
            "name": profile["name"],
            "email": profile["email"],
            "contact": profile["contact"],
            "preferences": profile["preferences"],
        },
        "answers": profile["answers"],
    }
    facts = []
    for item in profile["facts"]:
        fact = Fact(**{key: item[key] for key in (
            "id", "text", "source", "verified", "user_approved", "confidence",
        )})
        facts.append({
            "id": identity(profile["id"], version, fact.id),
            "profile_id": profile["id"],
            "version": version,
            "fact_id": fact.id,
            "kind": item["kind"],
            "text": fact.text,
            "source": fact.source,
            "verified": False,
            "user_approved": False,
            "confidence": 0.0,
        })
    return version, snapshot, facts


def apply_profile_restore(store, plan: dict, output_dir: str | Path, request_id: str) -> dict:
    """Apply a validated plan atomically as a new, unapproved profile.

    File bytes are staged under the configured uploads directory and linked to
    their final owned names inside the DB transaction. Any failed transaction
    removes only links created by this invocation.
    """
    profile, fingerprint, uploads, search_profiles = _validate_plan(plan, request_id)
    staged_directory = None
    staged_files = []
    created_paths: list[Path] = []
    try:
        with store.engine.begin() as connection:
            _lock_request(connection, request_id)
            replay = _read_receipt(connection, request_id, fingerprint)
            if replay:
                return replay
            if connection.execute(select(m.profiles.c.id).where(
                    m.profiles.c.id == profile["id"])).first():
                raise ProfileRestoreConflict("Restore target profile already exists")

            # Resolve the durable receipt before touching file storage. A retry
            # after a lost response must still return the original profile ID
            # even if the upload directory has since become unavailable.
            staged_directory, staged_files = _stage_files(uploads, output_dir)
            version, snapshot, facts = _profile_rows(profile, fingerprint)
            timestamp = now()
            connection.execute(m.profiles.insert().values(
                id=profile["id"], name=profile["name"], email=profile["email"], version=version,
            ))
            connection.execute(m.profile_versions.insert().values(**snapshot))
            if facts:
                connection.execute(m.facts.insert(), facts)

            for item, staged_path in staged_files:
                upload_id = identity(
                    "restored-upload", profile["id"], fingerprint,
                    item["source_id"], item["sha256"],
                )
                revision_id = identity("restored-resume-revision", profile["id"], upload_id)
                series_id = identity("restored-resume-series", profile["id"], item["source_id"])
                destination = staged_directory / f"{upload_id}.{item['format']}"
                try:
                    os.link(staged_path, destination)
                except FileExistsError:
                    raise ProfileRestoreFileError("Restore file destination already exists") from None
                except OSError:
                    raise ProfileRestoreFileError("Restore file could not be installed") from None
                created_paths.append(destination)
                os.chmod(destination, 0o600)
                connection.execute(m.uploaded_resumes.insert().values(
                    id=upload_id, profile_id=profile["id"], filename=item["filename"],
                    format=item["format"], media_type=item["media_type"],
                    size_bytes=item["size_bytes"], sha256=item["sha256"],
                    status="STORED_UNREVIEWED", created_at=timestamp,
                ))
                connection.execute(m.resume_revisions.insert().values(
                    id=revision_id, profile_id=profile["id"], upload_id=upload_id,
                    series_id=series_id, label=item["filename"], version=1,
                    created_at=timestamp, document_kind="BASE_RESUME",
                ))

            for index, item in enumerate(search_profiles):
                connection.execute(m.search_profiles.insert().values(
                    id=identity("restored-search-profile", profile["id"], fingerprint, index),
                    profile_id=profile["id"], name=item["name"], enabled=False,
                    criteria=item["criteria"], source_ids=[],
                    created_at=timestamp, updated_at=timestamp,
                ))

            connection.execute(m.audit.insert().values(
                id=_receipt_id(request_id), entity_id=profile["id"],
                action="PROFILE_RESTORED",
                payload={"request_id": request_id, "fingerprint": fingerprint,
                         "profile_id": profile["id"]},
                timestamp=timestamp,
            ))
        return {"profile_id": profile["id"], "request_id": request_id, "replayed": False}
    except ProfileRestoreError:
        for path in created_paths:
            if not path.is_symlink():
                path.unlink(missing_ok=True)
        raise
    except Exception:
        for path in created_paths:
            if not path.is_symlink():
                path.unlink(missing_ok=True)
        raise ProfileRestoreError("Restore could not be committed") from None
    finally:
        for _, path in staged_files:
            path.unlink(missing_ok=True)
