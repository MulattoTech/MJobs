"""Bounded, side-effect-free parsing and planning for v1 profile recovery."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from copy import deepcopy

from pydantic import ValidationError

from packages.domain.profile import ContactDetails, JobPreferences
from packages.persistence.recovery_history import SAFE_DISCOVERY_REASON_CODES


MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 10_000
MAX_IMPORT_DEPTH = 32
MAX_IMPORT_NODES = 100_000
MAX_IMPORT_STRING = 100_000
MAX_IMPORTED_FILE_BYTES = 5 * 1024 * 1024

SUPPORTED_EXPORT_TYPES = frozenset({"profile_backup", "profile_json_export"})
OMITTED_SECTIONS = (
    "provider_credentials",
    "browser_state",
    "worker_operations",
    "source_monitoring_and_feed_state",
    "posting_raw_diagnostics",
)


def _omitted_sections(payload, *, missing_resume_revision_uploads=False):
    """List intentional security exclusions and populated unsupported exports."""
    omitted = list(OMITTED_SECTIONS)
    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    if settings.get("ai_task_settings"):
        omitted.append("ai_task_provider_model")
    if missing_resume_revision_uploads:
        omitted.append("resume_revision_metadata_without_upload_bytes")
    return omitted


class ProfileRestoreError(ValueError):
    """A value-free, fixed-code validation failure for a restore payload."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code="RESTORE_INPUT_INVALID"):
    raise ProfileRestoreError(code)


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _reject_constant(_value):
    _fail()


def _normalize_payload(payload):
    if isinstance(payload, bytes):
        if len(payload) > MAX_IMPORT_BYTES:
            _fail("RESTORE_INPUT_TOO_LARGE")
        try:
            payload = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            _fail()
    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _fail()
        if len(raw) > MAX_IMPORT_BYTES:
            _fail("RESTORE_INPUT_TOO_LARGE")
        try:
            payload = json.loads(payload, object_pairs_hook=_json_pairs,
                                 parse_constant=_reject_constant)
        except ProfileRestoreError:
            raise
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            _fail()
    if not isinstance(payload, dict):
        _fail()

    nodes = 0

    def inspect(value, depth, field_name=None):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_IMPORT_NODES or depth > MAX_IMPORT_DEPTH:
            _fail("RESTORE_LIMIT_EXCEEDED")
        if isinstance(value, dict):
            if len(value) > MAX_IMPORT_ROWS:
                _fail("RESTORE_LIMIT_EXCEEDED")
            for key, nested in value.items():
                if not isinstance(key, str) or len(key) > 256:
                    _fail()
                inspect(nested, depth + 1, key)
        elif isinstance(value, list):
            if len(value) > MAX_IMPORT_ROWS:
                _fail("RESTORE_LIMIT_EXCEEDED")
            for nested in value:
                inspect(nested, depth + 1, field_name)
        elif isinstance(value, str):
            string_limit = MAX_IMPORT_BYTES if field_name == "content_base64" else MAX_IMPORT_STRING
            if len(value) > string_limit or "\x00" in value:
                _fail("RESTORE_LIMIT_EXCEEDED")
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                _fail()
        elif isinstance(value, float):
            if not math.isfinite(value):
                _fail()
        elif value is not None and type(value) not in (bool, int):
            _fail()

    inspect(payload, 0)
    try:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
        _fail()
    if len(canonical) > MAX_IMPORT_BYTES:
        _fail("RESTORE_INPUT_TOO_LARGE")
    return payload, canonical


def _string(value, *, max_length, allow_empty=False):
    if (not isinstance(value, str) or len(value) > max_length or "\x00" in value
            or (not allow_empty and not value.strip())):
        _fail()
    return value


def _identifier(value, *, maximum=128):
    value = _string(value, max_length=maximum)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
        _fail()
    return value


def _list(value, *, name):
    if not isinstance(value, list) or len(value) > MAX_IMPORT_ROWS:
        _fail("RESTORE_LIMIT_EXCEEDED")
    return value


def _object(value):
    if not isinstance(value, dict):
        _fail()
    return value


def _optional_profile_scope(row, source_id):
    if "profile_id" in row and _identifier(row["profile_id"]) != source_id:
        _fail("RESTORE_LINKAGE_INVALID")


def _source_profile(payload):
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        _fail("RESTORE_SCHEMA_UNSUPPORTED")
    if payload.get("export_type") not in SUPPORTED_EXPORT_TYPES:
        _fail("RESTORE_SCHEMA_UNSUPPORTED")

    source_id = _identifier(payload.get("profile_id"))
    profile = _object(payload.get("profile"))
    if _identifier(profile.get("id")) != source_id:
        _fail("RESTORE_LINKAGE_INVALID")
    name = _string(profile.get("name"), max_length=200)
    email = _string(profile.get("email"), max_length=320)
    version = _string(profile.get("version"), max_length=64)

    versions = _list(payload.get("profile_versions"), name="profile_versions")
    by_version = {}
    seen_rows = set()
    for row_value in versions:
        row = _object(row_value)
        row_id = _identifier(row.get("id"))
        _optional_profile_scope(row, source_id)
        row_version = _string(row.get("version"), max_length=64)
        if row_id in seen_rows or row_version in by_version:
            _fail("RESTORE_LINKAGE_INVALID")
        seen_rows.add(row_id)
        if row_id != _profile_version_id(source_id, row_version):
            _fail("RESTORE_LINKAGE_INVALID")
        by_version[row_version] = row
    current = by_version.get(version)
    if current is None:
        _fail("RESTORE_LINKAGE_INVALID")

    snapshot = _object(current.get("contact"))
    if snapshot.get("name") != name or snapshot.get("email") != email:
        _fail("RESTORE_LINKAGE_INVALID")
    if not isinstance(current.get("answers"), list):
        _fail("RESTORE_LINKAGE_INVALID")
    try:
        contact = ContactDetails.model_validate(snapshot.get("contact"), strict=True).model_dump()
        preferences = JobPreferences.model_validate(snapshot.get("preferences"), strict=True).model_dump()
    except (ValidationError, TypeError, ValueError):
        _fail("RESTORE_PROFILE_INVALID")
    return source_id, {"name": name, "email": email}, version, by_version, contact, preferences


def _profile_version_id(profile_id, version):
    data = json.dumps((profile_id, version), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _profile_fact_id(profile_id, version, fact_id):
    data = json.dumps((profile_id, version, fact_id), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _resume_revision_id(profile_id, series_id, upload_id):
    data = json.dumps(("resume-revision", profile_id, series_id, upload_id),
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _uploaded_resume_id(profile_id, digest):
    data = json.dumps(("uploaded-resume", profile_id, digest),
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _facts(payload, source_id, current_version, versions):
    rows = _list(payload.get("facts"), name="facts")
    seen_row_ids = set()
    facts_by_version = {}
    current = []
    all_facts = []
    for value in rows:
        row = _object(value)
        row_id = _identifier(row.get("id"))
        if row_id in seen_row_ids:
            _fail("RESTORE_LINKAGE_INVALID")
        _optional_profile_scope(row, source_id)
        seen_row_ids.add(row_id)
        version = _string(row.get("version"), max_length=64)
        if version not in versions:
            _fail("RESTORE_LINKAGE_INVALID")
        fact_id = _identifier(row.get("fact_id"))
        if row_id != _profile_fact_id(source_id, version, fact_id):
            _fail("RESTORE_LINKAGE_INVALID")
        kind = _string(row.get("kind"), max_length=32)
        text = _string(row.get("text"), max_length=10_000)
        source = _string(row.get("source"), max_length=1_000)
        if type(row.get("verified")) is not bool or type(row.get("user_approved")) is not bool:
            _fail()
        confidence = row.get("confidence")
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 100:
            _fail()
        version_facts = facts_by_version.setdefault(version, set())
        if fact_id in version_facts:
            _fail("RESTORE_LINKAGE_INVALID")
        version_facts.add(fact_id)
        all_facts.append({
            "version": version, "fact_id": fact_id, "kind": kind, "text": text,
            "source": f"Imported backup (unverified); original source: {source}"[:1_000],
        })
        if version == current_version:
            current.append({
                "id": fact_id,
                "text": text,
                "source": f"Imported backup (unverified); original source: {source}"[:1_000],
                "verified": False,
                "user_approved": False,
                "confidence": 0.0,
                "kind": kind,
            })

    stories = _list(payload.get("career_stories", []), name="career_stories")
    seen_story_ids = set()
    for story_value in stories:
        story = _object(story_value)
        story_id = _identifier(story.get("id"))
        if story_id in seen_story_ids:
            _fail("RESTORE_LINKAGE_INVALID")
        _optional_profile_scope(story, source_id)
        seen_story_ids.add(story_id)
        story_version = _string(story.get("profile_version"), max_length=64)
        if story_version not in versions:
            _fail("RESTORE_LINKAGE_INVALID")
        refs = _list(story.get("fact_ids"), name="career_story_fact_ids")
        normalized_refs = [_identifier(ref) for ref in refs]
        if (len(normalized_refs) != len(set(normalized_refs))
                or any(ref not in facts_by_version.get(story_version, set()) for ref in normalized_refs)):
            _fail("RESTORE_LINKAGE_INVALID")
    return current, facts_by_version, all_facts


def _version_snapshots(versions, current_version):
    snapshots = []
    for version, row in versions.items():
        raw = _object(row.get("contact"))
        try:
            contact = ContactDetails.model_validate(raw.get("contact"), strict=True).model_dump()
            preferences = JobPreferences.model_validate(raw.get("preferences"), strict=True).model_dump()
        except (ValidationError, TypeError, ValueError):
            _fail("RESTORE_PROFILE_INVALID")
        preferences["company_discovery"]["company_discovery_enabled"] = False
        preferences["application_route_preference"] = "USER_CHOICE"
        preferences["sensitive_answer_policies"] = {
            category: {"mode": "ALWAYS_REQUIRE_REVIEW", "response": None}
            for category in ("RACE_ETHNICITY", "RELIGION", "SEX_GENDER", "DISABILITY",
                             "VETERAN_STATUS", "OTHER_VOLUNTARY_DEMOGRAPHIC")
        }
        snapshots.append({
            "source_version": version,
            "contact": {"name": _string(raw.get("name"), max_length=200),
                        "email": _string(raw.get("email"), max_length=320),
                        "contact": contact, "preferences": preferences},
            "answers": _answers(row),
        })
    if not any(row["source_version"] == current_version for row in snapshots):
        _fail("RESTORE_LINKAGE_INVALID")
    return snapshots


def _answers(current_snapshot):
    answer_rows = _list(current_snapshot["answers"], name="answers")
    if len(answer_rows) > 200:
        _fail("RESTORE_LIMIT_EXCEEDED")
    seen = set()
    answers = []
    for value in answer_rows:
        row = _object(value)
        identifier = _identifier(row.get("id"))
        if identifier in seen:
            _fail("RESTORE_LINKAGE_INVALID")
        seen.add(identifier)
        text = _string(row.get("text"), max_length=10_000)
        source = _string(row.get("source"), max_length=1_000)
        if type(row.get("verified")) is not bool or type(row.get("user_approved")) is not bool:
            _fail()
        confidence = row.get("confidence")
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 100:
            _fail()
        answer = {
            "id": identifier,
            "text": text,
            "source": f"Imported backup (unverified); original source: {source}"[:1_000],
            "verified": False,
            "user_approved": False,
            "confidence": 0.0,
        }
        if "question" in row:
            answer["question"] = _string(row["question"], max_length=2_000)
        answers.append(answer)
    return answers


def _uploaded_files(payload, source_id):
    uploads = _list(payload.get("uploaded_resumes", []), name="uploaded_resumes")
    revisions = _list(payload.get("resume_revisions", []), name="resume_revisions")
    upload_by_id = {}
    for value in uploads:
        row = _object(value)
        identifier = _identifier(row.get("id"))
        _optional_profile_scope(row, source_id)
        if identifier in upload_by_id:
            _fail("RESTORE_LINKAGE_INVALID")
        filename = _string(row.get("filename"), max_length=200)
        if filename in {".", ".."} or any(character in filename for character in ("/", "\\", ":")):
            _fail("RESTORE_DOCUMENT_INVALID")
        file_format = row.get("format")
        media_type = row.get("media_type")
        expected_media = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                          "txt": "text/plain"}
        if file_format not in expected_media or media_type != expected_media[file_format]:
            _fail("RESTORE_DOCUMENT_INVALID")
        size = row.get("size_bytes")
        digest = row.get("sha256")
        if (type(size) is not int or not 0 < size <= MAX_IMPORTED_FILE_BYTES
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            _fail("RESTORE_DOCUMENT_INVALID")
        if identifier != _uploaded_resume_id(source_id, digest.casefold()):
            _fail("RESTORE_LINKAGE_INVALID")
        upload_by_id[identifier] = {
            "source_id": identifier, "filename": filename, "format": file_format,
            "media_type": media_type, "size_bytes": size, "sha256": digest.casefold(),
        }

    revisions_by_upload = {}
    seen_revision_ids = set()
    seen_series_versions = set()
    seen_series_uploads = set()
    for value in revisions:
        row = _object(value)
        revision_id = _identifier(row.get("id"))
        _optional_profile_scope(row, source_id)
        upload_id = _identifier(row.get("upload_id"))
        series_id = _identifier(row.get("series_id"))
        _string(row.get("label"), max_length=200)
        version = row.get("version")
        document_kind = row.get("document_kind")
        if (revision_id in seen_revision_ids or upload_id not in upload_by_id
                or type(version) is not int or version < 1
                or document_kind not in {"BASE_RESUME", "MASTER_RESUME", "TAILORED_RESUME",
                                         "COVER_LETTER", "APPLICATION_ANSWER_EXPORT",
                                         "SUPPORTING_DOCUMENT", "CERTIFICATION"}):
            _fail("RESTORE_LINKAGE_INVALID")
        if revision_id != _resume_revision_id(source_id, series_id, upload_id):
            _fail("RESTORE_LINKAGE_INVALID")
        if ((series_id, version) in seen_series_versions
                or (series_id, upload_id) in seen_series_uploads):
            _fail("RESTORE_LINKAGE_INVALID")
        seen_revision_ids.add(revision_id)
        seen_series_versions.add((series_id, version))
        seen_series_uploads.add((series_id, upload_id))
        revisions_by_upload.setdefault(upload_id, 0)
        revisions_by_upload[upload_id] += 1

    for upload_id in upload_by_id:
        if revisions_by_upload.get(upload_id, 0) == 0:
            _fail("RESTORE_LINKAGE_INVALID")

    documents = _list(payload.get("documents", []), name="documents")
    document_by_id = {}
    for value in documents:
        row = _object(value)
        identifier = _identifier(row.get("id"))
        checksums = _object(row.get("sha256", {}))
        if identifier in document_by_id:
            _fail("RESTORE_LINKAGE_INVALID")
        document_by_id[identifier] = checksums

    file_rows = _list(payload.get("document_files", []), name="document_files")
    seen_files = set()
    file_bytes_by_upload = {}
    file_bytes_by_document = {}
    file_ids = set(upload_by_id)
    if file_ids.intersection(document_by_id):
        _fail("RESTORE_LINKAGE_INVALID")
    for value in file_rows:
        row = _object(value)
        identifier = _identifier(row.get("id"))
        file_format = row.get("format")
        if file_format not in {"pdf", "docx", "txt"}:
            _fail("RESTORE_DOCUMENT_INVALID")
        identity = (identifier, file_format)
        if identity in seen_files:
            _fail("RESTORE_LINKAGE_INVALID")
        seen_files.add(identity)
        encoded = _string(row.get("content_base64"), max_length=MAX_IMPORT_BYTES)
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            _fail("RESTORE_DOCUMENT_INVALID")
        size = row.get("size_bytes")
        digest = row.get("sha256")
        if (type(size) is not int or size != len(content) or not 0 < size <= MAX_IMPORTED_FILE_BYTES
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            _fail("RESTORE_DOCUMENT_INVALID")
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != digest.casefold():
            _fail("RESTORE_DOCUMENT_INVALID")
        upload = upload_by_id.get(identifier)
        if upload is not None:
            if (file_format != upload["format"] or size != upload["size_bytes"]
                    or actual_digest != upload["sha256"] or identifier in file_bytes_by_upload):
                _fail("RESTORE_DOCUMENT_INVALID")
            file_bytes_by_upload[identifier] = {**upload, "content": content}
        else:
            expected = document_by_id.get(identifier, {}).get(file_format)
            if expected is None or not isinstance(expected, str) or expected.casefold() != actual_digest:
                _fail("RESTORE_LINKAGE_INVALID")
            file_bytes_by_document.setdefault(identifier, {})[file_format] = content

    if file_bytes_by_upload and len(file_bytes_by_upload) != len(upload_by_id):
        _fail("RESTORE_DOCUMENT_INVALID")
    generated_documents = []
    generated_documents_omitted = False
    for identifier, checksums in document_by_id.items():
        formats = set(checksums)
        files = file_bytes_by_document.get(identifier, {})
        if formats and formats == set(files):
            generated_documents.append({
                "id": identifier,
                "sha256": dict(checksums),
                "files": files,
            })
        elif formats:
            generated_documents_omitted = True
    return (list(file_bytes_by_upload.values()),
            len(upload_by_id) > 0 and not file_bytes_by_upload,
            generated_documents, generated_documents_omitted)


def _search_profiles(payload):
    settings = _object(payload.get("settings", {}))
    rows = _list(settings.get("search_profiles", []), name="search_profiles")
    seen_names = set()
    normalized = []
    for value in rows:
        row = _object(value)
        name = _string(row.get("name"), max_length=200)
        if name.casefold() in seen_names:
            _fail("RESTORE_LINKAGE_INVALID")
        seen_names.add(name.casefold())
        criteria = _object(row.get("criteria", {}))

        def without_scheduler(value):
            if isinstance(value, dict):
                return {key: without_scheduler(nested) for key, nested in value.items()
                        if key.casefold() != "scheduler"}
            if isinstance(value, list):
                return [without_scheduler(nested) for nested in value]
            return deepcopy(value)

        criteria = without_scheduler(criteria)
        normalized.append({"name": name, "criteria": criteria, "source_ids": [], "enabled": False})
    return normalized


def _safe_settings(payload):
    """Retain non-executable policies and force every AI task provider off."""
    settings = _object(payload.get("settings", {}))
    tasks = _list(settings.get("ai_task_settings", []), name="ai_task_settings")
    task_rows = []
    seen = set()
    for value in tasks:
        row = _object(value)
        task = _string(row.get("task"), max_length=64)
        if task in seen:
            _fail("RESTORE_LINKAGE_INVALID")
        seen.add(task)
        task_rows.append({"task": task, "updated_at": _string(row.get("updated_at"), max_length=64)})
    costs = _list(settings.get("ai_cost_policy", []), name="ai_cost_policy")
    confidence = _list(settings.get("answer_confidence_policy", []), name="answer_confidence_policy")
    if len(costs) > 1 or len(confidence) > 1:
        _fail("RESTORE_LINKAGE_INVALID")
    return {"ai_task_settings": task_rows,
            "ai_cost_policy": [dict(row) for row in costs],
            "answer_confidence_policy": [dict(row) for row in confidence]}


_HISTORY_FIELDS = {
    "jobs": {"id", "company", "requisition", "title", "description", "skills", "location",
             "remote_type", "application_url", "match_score", "application_state"},
    "application_routes": {"id", "job_id", "company_id", "provider", "tenant", "requisition", "url"},
    "job_matches": {"id", "job_id", "profile_version", "score", "missing", "components"},
    "applications": {"id", "job_id", "title", "company", "state", "target", "provider",
                      "application_url", "created_at", "submitted_at"},
    "application_route_choices": {"id", "application_id", "route_id", "requested_mode", "state", "created_at"},
    "documents": {"id", "application_id", "kind", "version", "profile_version", "fact_ids",
                   "sha256", "template", "created_at", "generation_model"},
    "attempts": {"id", "application_id", "state", "started_at"},
    "career_stories": {"id", "story_key", "revision", "profile_version", "kind", "title", "text",
                       "fact_ids", "fact_fingerprints", "tags", "allowed_uses", "review_status",
                       "review_note", "created_at", "reviewed_at"},
    "resume_workspace_revisions": {"id", "job_id", "version", "profile_version", "job_fingerprint",
                                   "template_id", "sections", "preview_fingerprint", "rendered_text",
                                   "source_refs", "created_at"},
    "interview_prep_packs": {"id", "job_id", "pack_version", "profile_version", "job_fingerprint",
                             "selected_story_revision_ids", "questions", "talking_points", "star_prompts",
                             "missing_evidence", "export_text", "created_at"},
    "ai_evaluation_runs": {"id", "profile_version", "policy", "task_provider", "task_model",
                           "evaluated_candidate_count", "created_at"},
    "ai_job_evaluations": {"id", "run_id", "job_id", "title", "company", "deterministic_score",
                           "category", "rationale", "created_at"},
    "ai_document_reviews": {"id", "run_id", "job_id", "reviewed_at"},
    "source_candidates": {"id", "domain", "display_name", "site_type", "state", "first_seen_at",
                          "last_seen_at", "collection_enabled", "review_reason", "integration_issue"},
    "source_candidate_reviews": {"id", "candidate_id", "state", "site_type", "reason",
                                 "api_feed_evidence_status", "api_feed_evidence_reference",
                                 "api_feed_evidence_note", "access_terms_note", "capabilities_limits_note",
                                 "expected_value_maintenance_note", "integration_issue", "reviewed_at"},
    "resume_revisions": {"id", "upload_id", "series_id", "label", "version", "created_at", "document_kind"},
    "uploaded_resumes": {"id", "filename", "format", "media_type", "size_bytes", "sha256", "status", "created_at"},
}


def _history_rows(payload, name, source_id):
    rows = _list(payload.get(name, []), name=name)
    seen = set()
    output = []
    for value in rows:
        row = _object(value)
        identifier = _identifier(row.get("id"))
        if identifier in seen or set(row) - _HISTORY_FIELDS[name] - {"profile_id"}:
            _fail("RESTORE_LINKAGE_INVALID")
        _optional_profile_scope(row, source_id)
        seen.add(identifier)
        output.append(dict(row))
    return output


def _restore_history(payload, source_id, versions, generated_documents):
    """Validate and retain the sanitized job/application/document graph only."""
    from services.data_export import _safe_url

    rows = {name: _history_rows(payload, name, source_id) for name in _HISTORY_FIELDS}
    jobs_by_id = {}
    for row in rows["jobs"]:
        for field, maximum in (("company", 200), ("title", 500), ("description", 50_000),
                               ("location", 1_000), ("remote_type", 32)):
            _string(row.get(field), max_length=maximum, allow_empty=field == "description")
        _identifier(row.get("requisition"))
        if not isinstance(row.get("skills"), (list, dict)):
            _fail("RESTORE_LINKAGE_INVALID")
        jobs_by_id[row["id"]] = row

    routes_by_id = {}
    routes_by_job = {}
    companies = {}
    company_for_job = {}
    for row in rows["application_routes"]:
        job_id = _identifier(row.get("job_id"))
        company_id = _identifier(row.get("company_id"))
        if job_id not in jobs_by_id:
            _fail("RESTORE_LINKAGE_INVALID")
        for field, maximum in (("provider", 32), ("tenant", 128), ("requisition", 128)):
            _string(row.get(field), max_length=maximum, allow_empty=True)
        safe_url = _safe_url(row.get("url"))
        if safe_url is None or len(safe_url) > 2000:
            continue
        row["url"] = safe_url
        if company_id in companies and companies[company_id] != jobs_by_id[job_id]["company"]:
            _fail("RESTORE_LINKAGE_INVALID")
        if job_id in company_for_job and company_for_job[job_id] != company_id:
            _fail("RESTORE_LINKAGE_INVALID")
        companies[company_id] = jobs_by_id[job_id]["company"]
        company_for_job[job_id] = company_id
        routes_by_id[row["id"]] = row
        routes_by_job.setdefault(job_id, []).append(row)

    # A job with no safe route cannot be represented by the existing read model.
    safe_job_ids = set(routes_by_job)
    jobs = [row for row in rows["jobs"] if row["id"] in safe_job_ids]
    jobs_by_id = {row["id"]: row for row in jobs}
    routes_by_id = {key: row for key, row in routes_by_id.items() if row["job_id"] in safe_job_ids}
    routes_by_job = {}
    for row in routes_by_id.values():
        routes_by_job.setdefault(row["job_id"], []).append(row)

    apps = []
    app_ids = set()
    for row in rows["applications"]:
        job_id = _identifier(row.get("job_id"))
        if job_id not in jobs_by_id:
            _fail("RESTORE_LINKAGE_INVALID")
        provider = _string(row.get("provider"), max_length=32, allow_empty=True)
        app_url = _safe_url(row.get("application_url"))
        route = next((item for item in routes_by_job[job_id]
                      if item["provider"] == provider and item["url"] == app_url), None)
        if route is None:
            continue
        _string(row.get("state"), max_length=64)
        _string(row.get("target"), max_length=32)
        _string(row.get("created_at"), max_length=64)
        if row.get("submitted_at") is not None:
            _string(row["submitted_at"], max_length=64)
        row["route_id"] = route["id"]
        apps.append(row)
        app_ids.add(row["id"])

    matches = []
    for row in rows["job_matches"]:
        if row.get("job_id") not in safe_job_ids:
            _fail("RESTORE_LINKAGE_INVALID")
        if row.get("profile_version") not in versions:
            _fail("RESTORE_LINKAGE_INVALID")
        matches.append(row)

    choices = []
    for row in rows["application_route_choices"]:
        if row.get("application_id") not in {item["id"] for item in apps}:
            _fail("RESTORE_LINKAGE_INVALID")
        if row.get("route_id") not in routes_by_id:
            continue
        choices.append(row)

    generated_by_id = {row["id"]: row for row in generated_documents}
    documents = []
    docs_omitted = False
    for row in rows["documents"]:
        if row.get("application_id") not in {item["id"] for item in apps}:
            _fail("RESTORE_LINKAGE_INVALID")
        generated = generated_by_id.get(row["id"])
        if generated is None:
            docs_omitted = True
            continue
        generated.update({key: value for key, value in row.items() if key != "id"})
        documents.append(generated)

    attempts = []
    for row in rows["attempts"]:
        if row.get("application_id") not in {item["id"] for item in apps}:
            _fail("RESTORE_LINKAGE_INVALID")
        _string(row.get("state"), max_length=64)
        _string(row.get("started_at"), max_length=64)
        attempts.append(row)

    stories = rows["career_stories"]
    for row in stories:
        for key, maximum in (("story_key", 128), ("kind", 32), ("title", 200), ("text", 8000),
                             ("review_status", 16), ("created_at", 64)):
            _string(row.get(key), max_length=maximum)
        if not isinstance(row.get("revision"), int) or isinstance(row.get("revision"), bool) or row["revision"] < 1:
            _fail("RESTORE_LINKAGE_INVALID")
        if row.get("profile_version") not in versions:
            _fail("RESTORE_LINKAGE_INVALID")
    workspace = rows["resume_workspace_revisions"]
    for row in workspace:
        if row.get("job_id") not in safe_job_ids or row.get("profile_version") not in versions:
            _fail("RESTORE_LINKAGE_INVALID")
    interview = rows["interview_prep_packs"]
    story_ids = {row["id"] for row in stories}
    for row in interview:
        if row.get("job_id") not in safe_job_ids or row.get("profile_version") not in versions:
            _fail("RESTORE_LINKAGE_INVALID")
        selected = row.get("selected_story_revision_ids", [])
        if not isinstance(selected, list) or any(identifier not in story_ids for identifier in selected):
            _fail("RESTORE_LINKAGE_INVALID")

    run_ids = {row["id"] for row in rows["ai_evaluation_runs"]}
    runs = []
    for row in rows["ai_evaluation_runs"]:
        if row.get("profile_version") not in versions:
            _fail("RESTORE_LINKAGE_INVALID")
        runs.append(row)
    evaluations = []
    for row in rows["ai_job_evaluations"]:
        if row.get("run_id") not in run_ids:
            _fail("RESTORE_LINKAGE_INVALID")
        if row.get("job_id") in safe_job_ids:
            evaluations.append(row)
    reviews = []
    for row in rows["ai_document_reviews"]:
        if row.get("run_id") not in run_ids:
            _fail("RESTORE_LINKAGE_INVALID")
        if row.get("job_id") in safe_job_ids:
            reviews.append(row)
    source_candidates = rows["source_candidates"]
    candidates_by_id = {row["id"] for row in source_candidates}
    for row in source_candidates:
        for key, maximum in (("domain", 253), ("display_name", 253), ("site_type", 32),
                             ("state", 32), ("first_seen_at", 64), ("last_seen_at", 64)):
            _string(row.get(key), max_length=maximum)
        if type(row.get("collection_enabled")) is not bool:
            _fail("RESTORE_LINKAGE_INVALID")
    candidate_reviews = rows["source_candidate_reviews"]
    for row in candidate_reviews:
        if row.get("candidate_id") not in candidates_by_id:
            _fail("RESTORE_LINKAGE_INVALID")
    upload_ids = {_identifier(row.get("id")) for row in _list(
        payload.get("uploaded_resumes", []), name="uploaded_resumes")}
    resume_revisions = rows["resume_revisions"]
    for row in resume_revisions:
        if row.get("upload_id") not in upload_ids:
            _fail("RESTORE_LINKAGE_INVALID")

    return {
        "jobs": jobs,
        "companies": [{"id": identifier, "name": name} for identifier, name in companies.items()],
        "application_routes": list(routes_by_id.values()),
        "job_matches": matches,
        "applications": apps,
        "application_route_choices": choices,
        "documents": documents,
        "attempts": attempts,
        "career_stories": stories,
        "resume_workspace_revisions": workspace,
        "interview_prep_packs": interview,
        "ai_evaluation_runs": runs,
        "ai_job_evaluations": evaluations,
        "ai_document_reviews": reviews,
        "source_candidates": source_candidates,
        "source_candidate_reviews": candidate_reviews,
        "company_discovery_history": _company_discovery_history(
            payload, source_id, jobs_by_id, companies,
        ),
        "resume_revisions": resume_revisions,
        "omitted": (
            len(jobs) != len(rows["jobs"])
            or len(routes_by_id) != len(rows["application_routes"])
            or len(apps) != len(rows["applications"])
            or len(choices) != len(rows["application_route_choices"])
            or docs_omitted
            or len(evaluations) != len(rows["ai_job_evaluations"])
            or len(reviews) != len(rows["ai_document_reviews"])
        ),
    }


def _company_discovery_history(payload, source_id, jobs_by_id, companies):
    """Convert live requests and existing archive rows to inert safe snapshots."""
    from services.data_export import _safe_url

    raw_requests = _list(payload.get("company_discovery_requests", []),
                         name="company_discovery_requests")
    archived = _list(payload.get("company_discovery_history", []),
                     name="company_discovery_history")
    rows = {}

    def add(source_identity, source_profile, refs, state, reasons, score, created,
            last_seen, revision, website, *, target_job_ref=None, target_company_ref=None,
            enforce_profile=True, body_metadata_status=None):
        source_identity = _identifier(source_identity)
        if not isinstance(refs, dict) or set(refs) - {
                "profile_id", "company_id", "company_source_id", "candidate_id",
                "trigger_job_id"}:
            _fail("RESTORE_LINKAGE_INVALID")
        for key, value in refs.items():
            if value is not None:
                refs[key] = _identifier(value)
        if ((enforce_profile and source_profile != source_id)
                or (not enforce_profile and source_profile != refs.get("profile_id"))):
            _fail("RESTORE_LINKAGE_INVALID")
        if state not in {"NEEDS_ORIGIN_REVIEW", "QUEUED", "DISMISSED", "UNKNOWN"}:
            state = "UNKNOWN"
        clean_reasons = _list(reasons if reasons is not None else [], name="discovery_reasons")
        if len(clean_reasons) > 50:
            _fail("RESTORE_LIMIT_EXCEEDED")
        clean_reasons = [_string(item, max_length=500) for item in clean_reasons]
        clean_reason_codes = [item for item in clean_reasons
                              if item in SAFE_DISCOVERY_REASON_CODES]
        if score is not None and (type(score) not in (int, float) or not math.isfinite(score)):
            _fail("RESTORE_LINKAGE_INVALID")
        created = _string(created, max_length=64, allow_empty=True)
        last_seen = _string(last_seen, max_length=64, allow_empty=True)
        if type(revision) is not int or revision < 0:
            _fail("RESTORE_LINKAGE_INVALID")
        safe_website = _safe_url(website) if website else None
        if target_job_ref is not None:
            target_job_ref = _identifier(target_job_ref)
        if target_company_ref is not None:
            target_company_ref = _identifier(target_company_ref)
        trigger_job = target_job_ref or refs.get("trigger_job_id")
        mapped_job = trigger_job if trigger_job in jobs_by_id else None
        company_id = target_company_ref or refs.get("company_id")
        mapped_company = company_id if company_id in companies else None
        metadata_status = ("COMPLETE" if all(refs.get(key) for key in (
            "company_id", "company_source_id", "candidate_id", "trigger_job_id",
        )) and len(clean_reason_codes) == len(clean_reasons) else "PARTIAL")
        if not enforce_profile and body_metadata_status == "PARTIAL":
            metadata_status = "PARTIAL"
        snapshot = {
            "kind": "company_discovery_request", "source_identity": source_identity,
            "schema_version": 1, "created_at": created,
            "payload": {
                "historical_refs": {key: value for key, value in refs.items() if value is not None},
                "target_job_id": mapped_job, "target_company_id": mapped_company,
                "state": state, "reasons": clean_reason_codes,
                "highest_match_score": score, "created_at": created,
                "last_seen_at": last_seen, "revision": revision,
                "selected_website": safe_website,
                "reference_status": ("LINKED" if mapped_job else
                                     "JOB_NOT_IN_BACKUP" if trigger_job else "NO_JOB_REFERENCE"),
                "metadata_status": metadata_status,
                "authorization_required": True,
            },
        }
        rows[source_identity] = snapshot

    archived_ids = {value["source_identity"] for value in archived
                    if isinstance(value, dict) and isinstance(value.get("source_identity"), str)}
    seen_raw_ids = set()
    for row_value in raw_requests:
        row = _object(row_value)
        if set(row) - {"id", "profile_id", "company_id", "company_source_id", "candidate_id",
                       "trigger_job_id", "profile_version", "reasons", "highest_match_score",
                       "created_at", "last_seen_at", "state", "revision", "selected_website"}:
            _fail("RESTORE_LINKAGE_INVALID")
        raw_id = _identifier(row.get("id"))
        if raw_id in seen_raw_ids:
            _fail("RESTORE_LINKAGE_INVALID")
        seen_raw_ids.add(raw_id)
        if row.get("id") in archived_ids:
            continue
        source_profile = row.get("profile_id") or source_id
        add(row.get("id"), source_profile, {
            "profile_id": source_profile,
            **{key: row.get(key) for key in ("company_id", "company_source_id",
                                              "candidate_id", "trigger_job_id")}
        }, row.get("state"), row.get("reasons"), row.get("highest_match_score"),
            row.get("created_at"), row.get("last_seen_at"), row.get("revision"),
            row.get("selected_website"))
    seen_archive_ids = set()
    for row_value in archived:
        row = _object(row_value)
        if set(row) != {"kind", "source_identity", "schema_version", "created_at", "payload"}:
            _fail("RESTORE_LINKAGE_INVALID")
        if (row.get("kind") != "company_discovery_request"
                or type(row.get("schema_version")) is not int
                or row.get("schema_version") != 1):
            _fail("RESTORE_SCHEMA_UNSUPPORTED")
        archived_identity = _identifier(row.get("source_identity"))
        if archived_identity in seen_archive_ids:
            _fail("RESTORE_LINKAGE_INVALID")
        seen_archive_ids.add(archived_identity)
        body = _object(row.get("payload"))
        if body.get("metadata_status") not in {"COMPLETE", "PARTIAL"}:
            _fail("RESTORE_LINKAGE_INVALID")
        if set(body) != {
                "historical_refs", "target_job_id", "target_company_id", "state", "reasons",
                "highest_match_score", "created_at", "last_seen_at", "revision",
                "selected_website", "reference_status", "metadata_status",
                "authorization_required"}:
            _fail("RESTORE_LINKAGE_INVALID")
        refs = _object(body.get("historical_refs", {}))
        if body.get("authorization_required") is not True:
            _fail("RESTORE_LINKAGE_INVALID")
        add(row.get("source_identity"), refs.get("profile_id"), refs,
            body.get("state"), body.get("reasons"), body.get("highest_match_score"),
            body.get("created_at", row.get("created_at")), body.get("last_seen_at", ""),
            body.get("revision"), body.get("selected_website"),
            target_job_ref=body.get("target_job_id"),
            target_company_ref=body.get("target_company_id"), enforce_profile=False,
            body_metadata_status=body.get("metadata_status"))
    output = list(rows.values())
    if len(output) > MAX_IMPORT_ROWS:
        _fail("RESTORE_LIMIT_EXCEEDED")
    total = 0
    for row in output:
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > 32 * 1024:
            _fail("RESTORE_LIMIT_EXCEEDED")
        total += len(encoded)
    if total > MAX_IMPORT_BYTES:
        _fail("RESTORE_LIMIT_EXCEEDED")
    return output


def _validated(payload):
    normalized, canonical = _normalize_payload(payload)
    source_id, source_profile, current_version, versions, contact, preferences = _source_profile(normalized)
    current_facts, facts_by_version, all_facts = _facts(normalized, source_id, current_version, versions)
    if len(current_facts) > 200:
        _fail("RESTORE_LIMIT_EXCEEDED")
    current_snapshot = versions[current_version]
    answers = _answers(current_snapshot)
    (uploaded_files, files_omitted, generated_documents,
     generated_documents_omitted) = _uploaded_files(normalized, source_id)
    searches = _search_profiles(normalized)
    safe_settings = _safe_settings(normalized)
    history = _restore_history(normalized, source_id, versions, generated_documents)
    available_upload_ids = {row["source_id"] for row in uploaded_files}
    kept_resume_revisions = [
        row for row in history["resume_revisions"]
        if row["upload_id"] in available_upload_ids
    ]
    missing_resume_revision_uploads = (
        len(kept_resume_revisions) != len(history["resume_revisions"])
    )
    history["resume_revisions"] = kept_resume_revisions
    history["omitted"] = history["omitted"] or missing_resume_revision_uploads
    history["profile_versions"] = _version_snapshots(versions, current_version)
    history["profile_facts"] = all_facts
    history["current_source_version"] = current_version
    generated_documents = history["documents"]

    preferences["company_discovery"]["company_discovery_enabled"] = False
    preferences["application_route_preference"] = "USER_CHOICE"
    preferences["sensitive_answer_policies"] = {
        category: {"mode": "ALWAYS_REQUIRE_REVIEW", "response": None}
        for category in ("RACE_ETHNICITY", "RELIGION", "SEX_GENDER", "DISABILITY",
                         "VETERAN_STATUS", "OTHER_VOLUNTARY_DEMOGRAPHIC")
    }
    try:
        preferences = JobPreferences.model_validate(preferences, strict=True).model_dump()
    except (ValidationError, TypeError, ValueError):
        _fail("RESTORE_PROFILE_INVALID")

    fingerprint = hashlib.sha256(canonical).hexdigest()
    warnings = ["AUTOMATION_NOT_RESTORED"]
    if current_facts:
        warnings.append("IMPORTED_FACTS_REQUIRE_REVIEW")
    if answers:
        warnings.append("IMPORTED_ANSWERS_REQUIRE_REVIEW")
    if searches:
        warnings.append("SEARCH_PROFILES_DISABLED")
    if history["career_stories"]:
        warnings.append("CAREER_STORIES_REQUIRE_REVIEW")
    if history["source_candidates"]:
        warnings.append("SOURCE_CANDIDATES_DISABLED")
    if history["company_discovery_history"]:
        warnings.append("COMPANY_DISCOVERY_HISTORY_ARCHIVED")
        if any(row["payload"]["reference_status"] != "LINKED"
               for row in history["company_discovery_history"]):
            warnings.append("DISCOVERY_HISTORY_REFERENCES_INCOMPLETE")
        if any(row["payload"]["metadata_status"] != "COMPLETE"
               for row in history["company_discovery_history"]):
            warnings.append("DISCOVERY_HISTORY_METADATA_PARTIAL")
    if files_omitted:
        warnings.append("UPLOADED_FILES_NOT_INCLUDED")
    if missing_resume_revision_uploads:
        warnings.append("RESUME_REVISION_METADATA_PARTIALLY_RESTORED")
    if generated_documents_omitted:
        warnings.append("GENERATED_DOCUMENTS_NOT_INCLUDED")
    if history["omitted"]:
        warnings.append("APPLICATION_HISTORY_PARTIALLY_RESTORED")
    if any(row["state"] not in {"PREPARED", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED", "AUTH_REQUIRED"}
           for row in history["applications"]):
        warnings.append("APPLICATION_STATES_REQUIRE_REVIEW")
    counts = {
        "facts": len(current_facts),
        "answers": len(answers),
        "uploaded_files": len(uploaded_files),
        "generated_documents": len(generated_documents),
        "jobs": len(history["jobs"]),
        "applications": len(history["applications"]),
        "search_profiles": len(searches),
        "ai_task_settings": len(safe_settings["ai_task_settings"]),
        "company_discovery_history": len(history["company_discovery_history"]),
    }
    return {
        "fingerprint": fingerprint,
        "source_id": source_id,
        "source_profile": source_profile,
        "current_version": current_version,
        "contact": contact,
        "preferences": preferences,
        "facts": current_facts,
        "answers": answers,
        "uploaded_files": uploaded_files,
        "generated_documents": generated_documents,
        "history": history,
        "search_profiles": searches,
        "safe_settings": safe_settings,
        "counts": counts,
        "warnings": warnings,
        "omitted_sections": _omitted_sections(
            normalized, missing_resume_revision_uploads=missing_resume_revision_uploads,
        ),
    }


def preview_profile_restore(payload) -> dict:
    """Validate a v1 profile export and return a value-free review preview."""
    result = _validated(payload)
    return {
        "fingerprint": result["fingerprint"],
        "source_profile": dict(result["source_profile"]),
        "counts": dict(result["counts"]),
        "warnings": list(result["warnings"]),
        "omitted_sections": list(result["omitted_sections"]),
    }


def build_restore_plan(payload, new_profile_id: str) -> dict:
    """Return a normalized new-profile plan; this function never writes data."""
    result = _validated(payload)
    new_id = _identifier(new_profile_id)
    if new_id == result["source_id"]:
        _fail("RESTORE_TARGET_INVALID")
    profile = {
        "id": new_id,
        "name": result["source_profile"]["name"],
        "email": result["source_profile"]["email"],
        "contact": result["contact"],
        "preferences": result["preferences"],
        "facts": result["facts"],
        "answers": result["answers"],
    }
    return {
        "fingerprint": result["fingerprint"],
        "profile": profile,
        "uploaded_files": result["uploaded_files"],
        "generated_documents": result["generated_documents"],
        "history": result["history"],
        "safe_settings": result["safe_settings"],
        "search_profiles": result["search_profiles"],
    }
