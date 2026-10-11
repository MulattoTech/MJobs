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


MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 10_000
MAX_IMPORT_DEPTH = 32
MAX_IMPORT_NODES = 100_000
MAX_IMPORT_STRING = 100_000
MAX_IMPORTED_FILE_BYTES = 5 * 1024 * 1024

SUPPORTED_EXPORT_TYPES = frozenset({"profile_backup", "profile_json_export"})
OMITTED_SECTIONS = (
    "job_history",
    "application_history",
    "application_routes",
    "application_attempts",
    "ai_evaluations",
    "source_discovery",
    "company_discovery",
    "generated_documents",
    "resume_workspace",
    "interview_prep",
)


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
    return current, facts_by_version


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

    if file_bytes_by_upload and len(file_bytes_by_upload) != len(upload_by_id):
        _fail("RESTORE_DOCUMENT_INVALID")
    return list(file_bytes_by_upload.values()), len(upload_by_id) > 0 and not file_bytes_by_upload


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


def _validated(payload):
    normalized, canonical = _normalize_payload(payload)
    source_id, source_profile, current_version, versions, contact, preferences = _source_profile(normalized)
    current_facts, facts_by_version = _facts(normalized, source_id, current_version, versions)
    if len(current_facts) > 200:
        _fail("RESTORE_LIMIT_EXCEEDED")
    current_snapshot = versions[current_version]
    answers = _answers(current_snapshot)
    uploaded_files, files_omitted = _uploaded_files(normalized, source_id)
    searches = _search_profiles(normalized)

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
    if files_omitted:
        warnings.append("UPLOADED_FILES_NOT_INCLUDED")
    counts = {
        "facts": len(current_facts),
        "answers": len(answers),
        "uploaded_files": len(uploaded_files),
        "search_profiles": len(searches),
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
        "search_profiles": searches,
        "counts": counts,
        "warnings": warnings,
    }


def preview_profile_restore(payload) -> dict:
    """Validate a v1 profile export and return a value-free review preview."""
    result = _validated(payload)
    return {
        "fingerprint": result["fingerprint"],
        "source_profile": dict(result["source_profile"]),
        "counts": dict(result["counts"]),
        "warnings": list(result["warnings"]),
        "omitted_sections": list(OMITTED_SECTIONS),
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
        "search_profiles": result["search_profiles"],
    }
