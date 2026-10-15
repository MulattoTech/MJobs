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
from packages.persistence import resume_workspace as workspace
from packages.persistence.recovery_history import (
    MAX_HISTORY_ROWS, insert_recovery_history, _safe_payload as _safe_recovery_payload,
)
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


def _validate_plan(plan: Any, request_id: str) -> tuple[dict, str, list[dict], list[dict], dict, dict]:
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
    history = plan.get("history", {})
    settings = plan.get("safe_settings", {"ai_task_settings": [], "ai_cost_policy": [],
                                           "answer_confidence_policy": []})
    if (not isinstance(facts, list) or len(facts) > 200
            or not isinstance(answers, list) or len(answers) > 200
            or not isinstance(uploads, list) or len(uploads) > 200
            or not isinstance(search_profiles, list) or len(search_profiles) > 100
            or not isinstance(history, dict) or not isinstance(settings, dict)):
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
    normalized_history = {}
    history_limits = {
        "companies": 10_000, "jobs": 10_000, "application_routes": 10_000,
        "job_matches": 10_000, "applications": 10_000,
        "application_route_choices": 10_000, "documents": 10_000, "attempts": 10_000,
        "career_stories": 10_000, "resume_workspace_revisions": 10_000,
        "interview_prep_packs": 10_000,
        "ai_evaluation_runs": 10_000, "ai_job_evaluations": 10_000,
        "ai_document_reviews": 10_000,
        "source_candidates": 10_000,
        "source_candidate_reviews": 10_000,
        "company_discovery_history": MAX_HISTORY_ROWS,
        "resume_revisions": 10_000,
        "profile_versions": 10_000, "profile_facts": 10_000,
    }
    for name, maximum in history_limits.items():
        rows = history.get(name, [])
        if not isinstance(rows, list) or len(rows) > maximum or any(not isinstance(row, dict) for row in rows):
            raise ProfileRestoreInvalidPlan("Restore history is invalid")
        normalized_history[name] = rows
    job_ids = {row.get("id") for row in normalized_history["jobs"]}
    company_ids = {row.get("id") for row in normalized_history["companies"]}
    route_ids = {row.get("id") for row in normalized_history["application_routes"]}
    application_ids = {row.get("id") for row in normalized_history["applications"]}
    document_ids = {row.get("id") for row in normalized_history["documents"]}
    if (None in job_ids or None in company_ids or None in route_ids or None in application_ids
            or None in document_ids
            or len(job_ids) != len(normalized_history["jobs"])
            or len(company_ids) != len(normalized_history["companies"])
            or len(route_ids) != len(normalized_history["application_routes"])
            or len(application_ids) != len(normalized_history["applications"])
            or len(document_ids) != len(normalized_history["documents"])):
        raise ProfileRestoreInvalidPlan("Restore history identity is invalid")
    for row in normalized_history["jobs"]:
        if not all(isinstance(row.get(key), str) for key in ("company", "title", "requisition", "description", "location", "remote_type")):
            raise ProfileRestoreInvalidPlan("Restore job is invalid")
        if not isinstance(row.get("skills"), (dict, list)):
            raise ProfileRestoreInvalidPlan("Restore job is invalid")
    for row in normalized_history["application_routes"]:
        if row.get("job_id") not in job_ids or row.get("company_id") not in company_ids:
            raise ProfileRestoreInvalidPlan("Restore route linkage is invalid")
        if not all(isinstance(row.get(key), str) for key in ("provider", "tenant", "requisition", "url")):
            raise ProfileRestoreInvalidPlan("Restore route is invalid")
    for row in normalized_history["applications"]:
        if (row.get("job_id") not in job_ids or row.get("route_id") not in route_ids
                or not isinstance(row.get("state"), str) or not isinstance(row.get("target"), str)):
            raise ProfileRestoreInvalidPlan("Restore application linkage is invalid")
    for row in normalized_history["job_matches"]:
        if row.get("job_id") not in job_ids:
            raise ProfileRestoreInvalidPlan("Restore match linkage is invalid")
    for row in normalized_history["application_route_choices"]:
        if row.get("application_id") not in application_ids or row.get("route_id") not in route_ids:
            raise ProfileRestoreInvalidPlan("Restore route choice linkage is invalid")
    for row in normalized_history["documents"]:
        if (row.get("application_id") not in application_ids or not isinstance(row.get("files"), dict)
                or not isinstance(row.get("fact_ids"), list)
                or any(not isinstance(value, str) or len(value) > 128 for value in row["fact_ids"])):
            raise ProfileRestoreInvalidPlan("Restore document linkage is invalid")
    for row in normalized_history["attempts"]:
        if row.get("application_id") not in application_ids:
            raise ProfileRestoreInvalidPlan("Restore attempt linkage is invalid")
    upload_source_ids = {row["source_id"] for row in normalized_uploads}
    revision_ids = {row.get("id") for row in normalized_history["resume_revisions"]}
    if (None in revision_ids or len(revision_ids) != len(normalized_history["resume_revisions"])
            or any(row.get("upload_id") not in upload_source_ids
                   for row in normalized_history["resume_revisions"])):
        raise ProfileRestoreInvalidPlan("Restore resume revision linkage is invalid")
    source_versions = {row.get("source_version") for row in normalized_history["profile_versions"]}
    current_source_version = history.get("current_source_version")
    if (None in source_versions or current_source_version not in source_versions
            or len(source_versions) != len(normalized_history["profile_versions"])):
        raise ProfileRestoreInvalidPlan("Restore profile version linkage is invalid")
    fact_keys = set()
    for row in normalized_history["profile_facts"]:
        key = (row.get("version"), row.get("fact_id"))
        if (key[0] not in source_versions or not isinstance(key[1], str) or key in fact_keys
                or not isinstance(row.get("kind"), str) or not isinstance(row.get("text"), str)
                or not isinstance(row.get("source"), str)):
            raise ProfileRestoreInvalidPlan("Restore historical fact linkage is invalid")
        fact_keys.add(key)

    history_source_ids = set()
    for row in normalized_history["company_discovery_history"]:
        if (row.get("kind") != "company_discovery_request"
                or row.get("schema_version") != 1
                or not isinstance(row.get("source_identity"), str)
                or not row["source_identity"] or len(row["source_identity"]) > 128
                or row["source_identity"] in history_source_ids):
            raise ProfileRestoreInvalidPlan("Restore discovery history identity is invalid")
        history_source_ids.add(row["source_identity"])
        try:
            payload = _safe_recovery_payload(row.get("payload"))
        except (TypeError, ValueError, RecursionError):
            raise ProfileRestoreInvalidPlan("Restore discovery history is invalid") from None
        refs = payload.get("historical_refs")
        if (not isinstance(refs, dict)
                or set(refs) - {"profile_id", "company_id", "company_source_id",
                                "candidate_id", "trigger_job_id"}
                or any(not isinstance(value, str) or not value or len(value) > 128
                       for value in refs.values())
                or payload.get("target_job_id") not in job_ids | {None}
                or payload.get("target_company_id") not in company_ids | {None}
                or payload.get("authorization_required") is not True):
            raise ProfileRestoreInvalidPlan("Restore discovery history references are invalid")
        row["payload"] = payload
        row["created_at"] = str(row.get("created_at") or "")[:64]

    document_bytes = 0
    for row in normalized_history["documents"]:
        expected = row.get("sha256")
        contents = row.get("files")
        if not isinstance(expected, dict) or set(expected) != set(contents):
            raise ProfileRestoreInvalidPlan("Restore document bytes are incomplete")
        for file_format, content in contents.items():
            digest = expected.get(file_format)
            if (file_format not in {"pdf", "docx", "txt"} or not isinstance(content, bytes)
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                    or hashlib.sha256(content).hexdigest() != digest):
                raise ProfileRestoreInvalidPlan("Restore document bytes are invalid")
            document_bytes += len(content)
            if document_bytes + total_bytes > 5 * 1024 * 1024:
                raise ProfileRestoreInvalidPlan("Restore files exceed the safe size limit")
            try:
                validate_resume(f"restored.{file_format}", {
                    "pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "txt": "text/plain",
                }[file_format], content)
            except Exception:
                raise ProfileRestoreInvalidPlan("Restore document bytes are invalid") from None

    if any(not isinstance(settings.get(name, []), list)
           for name in ("ai_task_settings", "ai_cost_policy", "answer_confidence_policy")):
        raise ProfileRestoreInvalidPlan("Restore settings are invalid")
    normalized_history["current_source_version"] = current_source_version
    return (normalized_profile, fingerprint, normalized_uploads, normalized_search_profiles,
            normalized_history, settings)


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


def _stage_files(uploads: list[dict], documents: list[dict], output_dir: str | Path):
    if not uploads and not documents:
        return None, []
    directory = _uploads_directory(output_dir)
    staged = []
    try:
        for item in uploads:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".restore-", delete=False) as handle:
                staged_path = Path(handle.name)
                staged.append(("upload", item, staged_path))
                handle.write(item["content"])
                handle.flush()
                os.fsync(handle.fileno())
        if documents:
            root = Path(output_dir).resolve(strict=True)
            for document in documents:
                for file_format, content in document["files"].items():
                    with tempfile.NamedTemporaryFile(dir=root, prefix=".restore-document-", delete=False) as handle:
                        staged_path = Path(handle.name)
                        staged.append(("document", {"document": document, "format": file_format}, staged_path))
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
        return directory, staged
    except OSError:
        for _, _, path in staged:
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
    profile, fingerprint, uploads, search_profiles, history, settings = _validate_plan(plan, request_id)
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
            staged_directory, staged_files = _stage_files(uploads, history["documents"], output_dir)
            version, snapshot, facts = _profile_rows(profile, fingerprint)
            timestamp = now()
            profile_version_ids = {
                row["source_version"]: (
                    version if row["source_version"] == history["current_source_version"]
                    else identity("restored-historical-profile-version", profile["id"],
                                  fingerprint, row["source_version"])
                ) for row in history["profile_versions"]
            }
            connection.execute(m.profiles.insert().values(
                id=profile["id"], name=profile["name"], email=profile["email"], version=version,
            ))
            for row in history["profile_versions"]:
                target_version = profile_version_ids[row["source_version"]]
                connection.execute(m.profile_versions.insert().values(
                    id=identity(profile["id"], target_version), profile_id=profile["id"],
                    version=target_version, contact=row["contact"], answers=row["answers"],
                ))
            imported_facts = [{
                "id": identity(profile["id"], profile_version_ids[row["version"]], row["fact_id"]),
                "profile_id": profile["id"], "version": profile_version_ids[row["version"]],
                "fact_id": row["fact_id"], "kind": row["kind"], "text": row["text"],
                "source": row["source"], "verified": False, "user_approved": False,
                "confidence": 0.0,
            } for row in history["profile_facts"]]
            if imported_facts:
                connection.execute(m.facts.insert(), imported_facts)

            # Recreate only the profile-associated graph in this isolated
            # namespace. Source identifiers are inputs to deterministic IDs,
            # never reused as target foreign keys.
            job_ids = {row["id"]: identity("restored-job", profile["id"], fingerprint, row["id"])
                       for row in history["jobs"]}
            company_ids = {row["id"]: identity("restored-company", profile["id"], fingerprint, row["id"])
                           for row in history["companies"]}
            route_ids = {row["id"]: identity("restored-route", profile["id"], fingerprint, row["id"])
                         for row in history["application_routes"]}
            app_ids = {row["id"]: identity(job_ids[row["job_id"]], profile["id"])
                       for row in history["applications"]}
            doc_ids = {row["id"]: identity("restored-document", profile["id"], fingerprint, row["id"])
                       for row in history["documents"]}
            for row in history["companies"]:
                connection.execute(m.companies.insert().values(
                    id=company_ids[row["id"]], name=row["name"], domain="",
                    parent_company_id=None, relationship_type=None,
                ))
            for row in history["jobs"]:
                source_route = next((item for item in history["application_routes"]
                                     if item["job_id"] == row["id"]), None)
                company_id = source_route["company_id"] if source_route else None
                company = next((item for item in history["companies"] if item["id"] == company_id), None)
                if company is None or company["name"] != row["company"]:
                    raise ProfileRestoreInvalidPlan("Restore job company is unavailable")
                connection.execute(m.jobs.insert().values(
                    id=job_ids[row["id"]], company_id=company_ids[company["id"]],
                    requisition=row["requisition"], title=row["title"],
                    description=row["description"], skills=row["skills"],
                    location=row["location"], remote_type=row["remote_type"], attributes={},
                ))
            archived_discovery = []
            for row in history["company_discovery_history"]:
                payload = dict(row["payload"])
                source_job_id = payload.get("target_job_id")
                source_company_id = payload.get("target_company_id")
                payload["target_job_id"] = job_ids.get(source_job_id)
                payload["target_company_id"] = company_ids.get(source_company_id)
                if payload.get("reference_status") == "LINKED" and payload["target_job_id"] is None:
                    payload["reference_status"] = "JOB_NOT_IN_BACKUP"
                archived_discovery.append({**row, "payload": payload})
            insert_recovery_history(connection, profile["id"], archived_discovery)
            for row in history["application_routes"]:
                company = next(item for item in history["companies"] if item["id"] == row["company_id"])
                connection.execute(m.routes.insert().values(
                    id=route_ids[row["id"]], job_id=job_ids[row["job_id"]],
                    company_id=company_ids[company["id"]], provider=row["provider"],
                    tenant=row["tenant"], requisition=row["requisition"], url=row["url"],
                ))
            for row in history["job_matches"]:
                connection.execute(m.matches.insert().values(
                    id=identity("restored-match", profile["id"], fingerprint, row["id"]),
                    job_id=job_ids[row["job_id"]], profile_id=profile["id"],
                    profile_version=profile_version_ids[row["profile_version"]],
                    score=row["score"], missing=row["missing"],
                    components=row["components"],
                ))
            ambiguous_states = {"SUBMITTING", "CONFIRMED", "SUBMISSION_CONFIRMED",
                                "SUBMISSION_UNCONFIRMED", "APPLICATION_SUBMITTED"}
            terminal_states = {"FAILED", "CANCELLED", "REJECTED", "BLOCKED",
                               "DEAD_LETTERED", "DISCARDED", "UNKNOWN"}
            for row in history["applications"]:
                state = row["state"]
                submitted_at = row.get("submitted_at")
                if state in ambiguous_states:
                    state, submitted_at = "SUBMISSION_UNCONFIRMED", None
                elif state not in terminal_states and state != "REVIEW_REQUIRED":
                    state, submitted_at = "REVIEW_REQUIRED", None
                connection.execute(m.applications.insert().values(
                    id=app_ids[row["id"]], job_id=job_ids[row["job_id"]],
                    route_id=route_ids[row["route_id"]], profile_id=profile["id"],
                    profile_version=version, state=state, target=row["target"],
                    fact_ids=[], trace=[], document_version=max(
                        (doc.get("version", 1) for doc in history["documents"]
                         if doc["application_id"] == row["id"]), default=1,
                    ), confirmation=None,
                    created_at=row["created_at"], submitted_at=submitted_at,
                ))
            for row in history["application_route_choices"]:
                route = next(item for item in history["application_routes"] if item["id"] == row["route_id"])
                key = f"{route['provider']}|{route['tenant']}|{route['url']}"
                connection.execute(m.application_route_choices.insert().values(
                    id=identity("restored-route-choice", profile["id"], fingerprint, row["id"]),
                    application_id=app_ids[row["application_id"]], route_id=route_ids[row["route_id"]],
                    underlying_key=key[:2200], requested_mode=row["requested_mode"],
                    state=row["state"], created_at=row["created_at"],
                ))
            root = None
            for kind, item, staged_path in staged_files:
                if kind != "document":
                    continue
                if root is None:
                    root = Path(output_dir).resolve(strict=True)
                document = item["document"]
                app_id = app_ids[document["application_id"]]
                app_dir = root / app_id
                app_dir.mkdir(mode=0o700, exist_ok=True)
                if app_dir.is_symlink() or app_dir.resolve().parent != root:
                    raise ProfileRestoreFileError("Restore document destination is unsafe")
                destination = app_dir / f"{doc_ids[document['id']]}.{item['format']}"
                try:
                    os.link(staged_path, destination)
                except (FileExistsError, OSError):
                    raise ProfileRestoreFileError("Restore document could not be installed") from None
                created_paths.append(destination)
                os.chmod(destination, 0o600)
            for row in history["documents"]:
                app_id = app_ids[row["application_id"]]
                files = {fmt: f"{doc_ids[row['id']]}.{fmt}" for fmt in row["files"]}
                checksums = {fmt: hashlib.sha256(data).hexdigest() for fmt, data in row["files"].items()}
                connection.execute(m.documents.insert().values(
                    id=doc_ids[row["id"]], application_id=app_id, kind=row["kind"],
                    version=row["version"], profile_version=profile_version_ids[row["profile_version"]], files=files,
                    fact_ids=row["fact_ids"], sha256=checksums, template=row["template"],
                    created_at=row.get("created_at"), generation_model=row.get("generation_model"),
                    generation_context=None,
                ))
            for row in history["attempts"]:
                # Attempt execution traces and retry steps are intentionally
                # not portable. Preserve only terminal historical state.
                state = row["state"] if row["state"] != "RUNNING" else "UNKNOWN"
                connection.execute(m.attempts.insert().values(
                    id=identity("restored-attempt", profile["id"], fingerprint, row["id"]),
                    application_id=app_ids[row["application_id"]], state=state,
                    steps=[], started_at=row["started_at"],
                ))
            evaluation_run_ids = {
                row["id"]: identity("restored-ai-evaluation-run", profile["id"], fingerprint, row["id"])
                for row in history["ai_evaluation_runs"]
            }
            for row in history["ai_evaluation_runs"]:
                connection.execute(m.ai_evaluation_runs.insert().values(
                    id=evaluation_run_ids[row["id"]], profile_id=profile["id"],
                    profile_version=profile_version_ids[row["profile_version"]], policy=row["policy"],
                    task_provider=row["task_provider"], task_model=row["task_model"],
                    evaluated_candidate_count=row["evaluated_candidate_count"],
                    created_at=row["created_at"],
                ))
            for row in history["ai_job_evaluations"]:
                connection.execute(m.ai_job_evaluations.insert().values(
                    id=identity("restored-ai-job-evaluation", profile["id"], fingerprint, row["id"]),
                    run_id=evaluation_run_ids[row["run_id"]], profile_id=profile["id"],
                    job_id=job_ids[row["job_id"]], title=row["title"], company=row["company"],
                    deterministic_score=row["deterministic_score"], category=row["category"],
                    rationale=row["rationale"], job_fingerprint=None, created_at=row["created_at"],
                ))
            for row in history["ai_document_reviews"]:
                connection.execute(m.ai_document_reviews.insert().values(
                    id=identity("restored-ai-document-review", profile["id"], fingerprint, row["id"]),
                    profile_id=profile["id"], run_id=evaluation_run_ids[row["run_id"]],
                    job_id=job_ids[row["job_id"]], reviewed_at=row["reviewed_at"],
                ))
            candidate_ids = {}
            for row in history["source_candidates"]:
                state = row["state"] if row["state"] in {"DISCOVERED", "NEEDS_REVIEW", "BLOCKED", "REJECTED"} else "NEEDS_REVIEW"
                candidate_ids[row["id"]] = identity(
                    "restored-source-candidate", profile["id"], fingerprint, row["id"]
                )
                connection.execute(m.source_candidates.insert().values(
                    id=candidate_ids[row["id"]],
                    profile_id=profile["id"], domain=row["domain"], display_name=row["display_name"],
                    site_type=row["site_type"], state=state, first_seen_at=row["first_seen_at"],
                    last_seen_at=row["last_seen_at"], collection_enabled=False,
                    review_reason=(row.get("review_reason") or "")[:1000],
                    integration_issue=row.get("integration_issue"),
                ))
            for row in history["source_candidate_reviews"]:
                connection.execute(m.source_candidate_reviews.insert().values(
                    id=identity("restored-source-candidate-review", profile["id"], fingerprint, row["id"]),
                    profile_id=profile["id"], candidate_id=candidate_ids[row["candidate_id"]],
                    state=row["state"], site_type=row["site_type"], reason=row["reason"],
                    api_feed_evidence_status=row["api_feed_evidence_status"],
                    api_feed_evidence_reference=row["api_feed_evidence_reference"],
                    api_feed_evidence_note=row["api_feed_evidence_note"],
                    access_terms_note=row["access_terms_note"],
                    capabilities_limits_note=row["capabilities_limits_note"],
                    expected_value_maintenance_note=row["expected_value_maintenance_note"],
                    integration_issue=row.get("integration_issue"), reviewed_at=row["reviewed_at"],
                ))
            story_ids = {row["id"]: identity("restored-story", profile["id"], fingerprint, row["id"])
                         for row in history["career_stories"]}
            for row in history["career_stories"]:
                connection.execute(m.career_stories.insert().values(
                    id=story_ids[row["id"]], profile_id=profile["id"], story_key=row["story_key"],
                    revision=row["revision"], profile_version=profile_version_ids[row["profile_version"]], kind=row["kind"],
                    title=row["title"], text=row["text"], fact_ids=row["fact_ids"],
                    fact_fingerprints=row["fact_fingerprints"], tags=row["tags"],
                    allowed_uses=row["allowed_uses"], review_status="PENDING", review_note=None,
                    created_at=row["created_at"], reviewed_at=None,
                ))
            for row in history["resume_workspace_revisions"]:
                # Imported workspace content is historical and inert. Its
                # source references are retained as user data, but bindings
                # point at the newly remapped target job and current version.
                from datetime import datetime
                created = row["created_at"]
                if isinstance(created, str):
                    created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                connection.execute(workspace.resume_workspace_revisions.insert().values(
                    id=identity("restored-workspace", profile["id"], fingerprint, row["id"]),
                    profile_id=profile["id"], job_id=job_ids[row["job_id"]], version=row["version"],
                    profile_version=profile_version_ids[row["profile_version"]], job_fingerprint=row["job_fingerprint"],
                    template_id=row["template_id"], sections=row["sections"],
                    preview_fingerprint=row["preview_fingerprint"], rendered_text=row["rendered_text"],
                    source_refs=row["source_refs"], request_id=None, payload_fingerprint=None,
                    created_at=created,
                ))
            for row in history["interview_prep_packs"]:
                connection.execute(m.interview_prep_packs.insert().values(
                    id=identity("restored-interview-prep", profile["id"], fingerprint, row["id"]),
                    profile_id=profile["id"], job_id=job_ids[row["job_id"]],
                    pack_version=row["pack_version"], profile_version=profile_version_ids[row["profile_version"]],
                    job_fingerprint=row["job_fingerprint"],
                    selected_story_revision_ids=[story_ids[item] for item in row["selected_story_revision_ids"]],
                    questions=row["questions"], talking_points=row["talking_points"],
                    star_prompts=row["star_prompts"], missing_evidence=row["missing_evidence"],
                    export_text=row["export_text"], created_at=row["created_at"],
                ))

            upload_ids = {}
            for kind, item, staged_path in staged_files:
                if kind != "upload":
                    continue
                # Keep the canonical profile+digest identity used by future
                # backup validation so restore -> export -> restore remains valid.
                upload_id = identity("uploaded-resume", profile["id"], item["sha256"])
                upload_ids[item["source_id"]] = upload_id
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
            series_ids = {row["series_id"]: identity(
                "restored-resume-series", profile["id"], fingerprint, row["series_id"]
            ) for row in history["resume_revisions"]}
            for row in history["resume_revisions"]:
                connection.execute(m.resume_revisions.insert().values(
                    id=identity("resume-revision", profile["id"], series_ids[row["series_id"]],
                                upload_ids[row["upload_id"]]),
                    profile_id=profile["id"], upload_id=upload_ids[row["upload_id"]],
                    series_id=series_ids[row["series_id"]], label=row["label"],
                    version=row["version"], created_at=row["created_at"],
                    document_kind=row["document_kind"],
                ))

            for index, item in enumerate(search_profiles):
                connection.execute(m.search_profiles.insert().values(
                    id=identity("restored-search-profile", profile["id"], fingerprint, index),
                    profile_id=profile["id"], name=item["name"], enabled=False,
                    criteria=item["criteria"], source_ids=[],
                    created_at=timestamp, updated_at=timestamp,
                ))
            for row in settings.get("ai_task_settings", []):
                connection.execute(m.ai_task_settings.insert().values(
                    id=identity("restored-ai-task-setting", profile["id"], fingerprint, row["task"]),
                    profile_id=profile["id"], task=row["task"], provider="disabled", model=None,
                    updated_at=row["updated_at"],
                ))
            if settings.get("ai_cost_policy"):
                row = settings["ai_cost_policy"][0]
                connection.execute(m.ai_cost_policies.insert().values(
                    id=identity("restored-ai-cost-policy", profile["id"], fingerprint),
                    profile_id=profile["id"], minimum_score=row["minimum_score"],
                    max_input_candidates=row["max_input_candidates"], max_ai_candidates=row["max_ai_candidates"],
                    max_document_candidates=row["max_document_candidates"], updated_at=row["updated_at"],
                ))
            if settings.get("answer_confidence_policy"):
                row = settings["answer_confidence_policy"][0]
                connection.execute(m.answer_confidence_policies.insert().values(
                    id=identity("restored-answer-confidence-policy", profile["id"], fingerprint),
                    profile_id=profile["id"], minimum_saved_answer_confidence=row["minimum_saved_answer_confidence"],
                    minimum_question_match_confidence=row["minimum_question_match_confidence"],
                    updated_at=row["updated_at"],
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
        for _, _, path in staged_files:
            path.unlink(missing_ok=True)
