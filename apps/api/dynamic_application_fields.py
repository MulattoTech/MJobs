"""Read-only, profile-scoped dynamic application field metadata."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound

from apps.api.application_form_review import (
    _KIND as _FORM_REVIEW_KIND,
    _application_route,
    _frozen_review_input,
    _input_fingerprint,
)
from packages.persistence import models as m
from services.applications.preflight import latest_preflight


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_FIELD_KINDS = {"text", "select", "boolean", "choice", "email", "number", "date", "file"}
_MAX_FIELDS = 200
_MAX_CHOICES = 100
_MAX_TEXT = 500
_MAX_SECTIONS = 32
_MAX_REPEATABLE_ROWS = 20


def _snapshot(report: Mapping) -> dict:
    return {
        "profile_version": report.get("profile_version"),
        "document_version": report.get("document_version"),
        "route_id": report.get("route_id"),
        "metadata_fingerprint": report.get("metadata_fingerprint"),
    }


def _field_type(value) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.casefold()
    aliases = {
        "textarea": "text",
        "select-one": "select",
        "select-multiple": "select",
        "radio": "choice",
        "checkbox": "boolean",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _FIELD_KINDS else None


def _workday_field_type(value) -> str | None:
    """Normalize Workday envelope controls without changing generic mappings."""
    if isinstance(value, str) and value.casefold() in {"tel", "url", "password"}:
        return "text"
    return _field_type(value)


def _fields(report: Mapping) -> list[dict] | None:
    """Project a complete safe metadata snapshot, or fail closed as a whole."""
    source = report.get("dynamic_fields", report.get("mappings"))
    if not isinstance(source, list) or not source or len(source) > _MAX_FIELDS:
        return None
    fields = []
    seen = set()
    required_keys = {"required", "visible", "repeatable"}
    for item in source:
        if not isinstance(item, Mapping):
            return None
        field_id = item.get("field_id", item.get("question_id"))
        label = item.get("label", item.get("question"))
        kind = _field_type(item.get("type") or item.get("kind")
                           or item.get("input_type") or item.get("tag"))
        dependent_on = item.get("depends_on", item.get("dependent_on"))
        choices = item.get("options", item.get("choices"))
        section_id = item.get("section_id", item.get("optional_section_id"))
        if section_id is None and isinstance(item.get("optional_sections"), list):
            section_id = item["optional_sections"][0] if len(item["optional_sections"]) == 1 else None
        repeatable_group = item.get("repeatable_group")
        min_rows = item.get("repeatable_min_rows")
        max_rows = item.get("repeatable_max_rows")
        sensitivity = item.get("sensitivity", "UNKNOWN")
        ambiguous = item.get("ambiguous") is True
        type_explicit = item.get("type_explicit") is True
        manual_review = (item.get("manual_review_required") is not False
                         or sensitivity != "STANDARD" or ambiguous or not type_explicit)
        if (not isinstance(field_id, str) or not _IDENTIFIER.fullmatch(field_id)
                or field_id in seen or not required_keys.issubset(item)
                or not isinstance(label, str) or not label.strip() or len(label) > _MAX_TEXT):
            return None
        if (kind is None
                or type(item.get("required")) is not bool
                or type(item.get("visible")) is not bool
                or type(item.get("repeatable")) is not bool
                or (dependent_on is not None and (
                    not isinstance(dependent_on, str) or not _IDENTIFIER.fullmatch(dependent_on)))
                or (section_id is not None and (
                    not isinstance(section_id, str) or not _IDENTIFIER.fullmatch(section_id)))
                or (repeatable_group is not None and (
                    not isinstance(repeatable_group, str)
                    or not _IDENTIFIER.fullmatch(repeatable_group)))
                or ((min_rows is None) != (max_rows is None))
                or ((min_rows is not None or max_rows is not None)
                    and (repeatable_group is None or item.get("repeatable") is not True
                         or type(min_rows) is not int or type(max_rows) is not int
                         or min_rows < 0 or max_rows < 1 or min_rows > max_rows
                         or max_rows > _MAX_REPEATABLE_ROWS))
                or not isinstance(choices, list) or len(choices) > _MAX_CHOICES
                or sensitivity not in {"STANDARD", "SENSITIVE", "CREDENTIAL", "UNKNOWN"}
                or ("supported" in item and type(item.get("supported")) is not bool)
                or ("manual_review_required" in item
                    and type(item.get("manual_review_required")) is not bool)
                or ("ambiguous" in item and type(item.get("ambiguous")) is not bool)
                or any(not isinstance(choice, str) or not choice.strip() or len(choice) > _MAX_TEXT
                       for choice in choices)):
            return None
        normalized_choices = [" ".join(choice.split()) for choice in choices]
        if len(set(normalized_choices)) != len(normalized_choices):
            return None
        seen.add(field_id)
        supported_kind = kind in {"text", "select", "boolean"}
        supported = (item.get("supported") is True and not manual_review and supported_kind)
        field = {
            "id": field_id,
            "label": ("Sensitive or ambiguous question requires manual review"
                      if manual_review and sensitivity != "STANDARD" or ambiguous
                      else "Unsupported field requires manual review" if not supported_kind
                      else " ".join(label.split())),
            "type": kind,
            "required": item["required"],
            "visible": item["visible"],
            "depends_on": dependent_on,
            "options": [] if manual_review else normalized_choices,
            "repeatable": item["repeatable"],
            "supported": supported,
            "sensitive": sensitivity != "STANDARD",
            "manual_review_required": manual_review or not supported_kind,
        }
        if item.get("condition") is not None:
            condition = item["condition"]
            if (not isinstance(condition, Mapping) or set(condition) != {"question_id", "equals"}
                    or condition.get("question_id") != dependent_on
                    or not isinstance(condition.get("equals"), str)
                    or len(condition["equals"]) > _MAX_TEXT):
                return None
            field["condition"] = {"question_id": dependent_on,
                                  "equals": " ".join(condition["equals"].split())}
        if item.get("options_by") is not None:
            options_by = item["options_by"]
            if (not isinstance(options_by, Mapping)
                    or set(options_by) != {"question_id", "choices"}
                    or options_by.get("question_id") != dependent_on
                    or not isinstance(options_by.get("choices"), Mapping)
                    or not 1 <= len(options_by["choices"]) <= _MAX_CHOICES):
                return None
            by_value = {}
            for parent_value, options in options_by["choices"].items():
                if (not isinstance(parent_value, str) or len(parent_value) > _MAX_TEXT
                        or not isinstance(options, list) or not options
                        or len(options) > _MAX_CHOICES
                        or any(not isinstance(value, str) or not value.strip()
                               or len(value) > _MAX_TEXT for value in options)):
                    return None
                by_value[" ".join(parent_value.split())] = [" ".join(value.split()) for value in options]
            field["options_by"] = by_value
        if section_id is not None:
            field["section_id"] = section_id
        if repeatable_group is not None:
            field.update({
                "repeatable_group": repeatable_group,
                "repeatable_min_rows": min_rows,
                "repeatable_max_rows": max_rows,
            })
        fields.append(field)
    if any(field["depends_on"] is not None and field["depends_on"] not in seen
           for field in fields):
        return None
    parents = {field["id"]: field["depends_on"] for field in fields}
    visiting = set()
    visited = set()

    def has_cycle(field_id: str) -> bool:
        if field_id in visiting:
            return True
        if field_id in visited:
            return False
        visiting.add(field_id)
        parent = parents[field_id]
        if parent is not None and has_cycle(parent):
            return True
        visiting.remove(field_id)
        visited.add(field_id)
        return False

    if any(has_cycle(field_id) for field_id in parents):
        return None
    return fields


def _sections(report: Mapping, fields: list[dict]) -> list[dict] | None:
    source = report.get("optional_sections", [])
    if isinstance(source, Mapping):
        if any(not isinstance(value, Mapping) for value in source.values()):
            return None
        source = [dict(value, section_id=section_id)
                  for section_id, value in source.items()]
    if not isinstance(source, list) or len(source) > _MAX_SECTIONS:
        return None
    field_by_id = {field["id"]: field for field in fields}
    sections = []
    seen = set()
    assigned_fields = set()
    for item in source:
        if not isinstance(item, Mapping):
            return None
        section_id = item.get("section_id", item.get("id"))
        label = item.get("label")
        field_ids = item.get("question_ids", item.get("field_ids"))
        if (not isinstance(section_id, str) or not _IDENTIFIER.fullmatch(section_id)
                or section_id in seen
                or (label is not None and (not isinstance(label, str)
                    or not label.strip() or len(label) > _MAX_TEXT))
                or not isinstance(field_ids, list) or not field_ids
                or len(field_ids) > _MAX_FIELDS
                or any(not isinstance(field_id, str) for field_id in field_ids)
                or len(set(field_ids)) != len(field_ids)
                or any(field_id not in field_by_id for field_id in field_ids)
                or assigned_fields.intersection(field_ids)
                or any(field_by_id[field_id].get("section_id") not in (None, section_id)
                       for field_id in field_ids)
                or any(key in item and type(item[key]) is not bool
                       for key in ("required", "visible", "expanded"))):
            return None
        seen.add(section_id)
        assigned_fields.update(field_ids)
        for field_id in field_ids:
            field_by_id[field_id]["section_id"] = section_id
        section = {"id": section_id, "field_ids": field_ids}
        if isinstance(label, str):
            section["label"] = " ".join(label.split())
        for key in ("required", "visible", "expanded"):
            if key in item:
                section[key] = item[key]
        sections.append(section)
    if any(field.get("section_id") is not None and field["section_id"] not in seen
           for field in fields):
        return None
    return sections


def _repeatable_groups(fields: list[dict]) -> list[dict] | None:
    groups = {}
    for field in fields:
        group_id = field.get("repeatable_group")
        bounds = (field.get("repeatable_min_rows"), field.get("repeatable_max_rows"))
        if group_id is None:
            continue
        if bounds == (None, None):
            return None
        group = groups.setdefault(group_id, {"bounds": bounds, "field_ids": []})
        if group["bounds"] != bounds:
            return None
        group["field_ids"].append(field["id"])
    return [{
        "id": group_id,
        "field_ids": sorted(group["field_ids"]),
        "minimum_rows": group["bounds"][0],
        "maximum_rows": group["bounds"][1],
    } for group_id, group in sorted(groups.items())]


def _field_fingerprint(profile_id: str, application_id: str,
                       application: Mapping, report_snapshot: Mapping,
                       fields: list[dict], optional_sections: list[dict],
                       repeatable_groups: list[dict]) -> str:
    content = {
        "schema_version": 1,
        "profile_id": profile_id,
        "application_id": application_id,
        "profile_version": application.get("profile_version"),
        "document_version": application.get("document_version"),
        "route_id": application.get("route_id"),
        "source_fingerprint": report_snapshot.get("metadata_fingerprint"),
        "fields": fields,
        "optional_sections": optional_sections,
        "repeatable_groups": repeatable_groups,
    }
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _latest_workday_dynamic_review(store, profile_id: str, application_id: str,
                                   application: Mapping):
    """Read only the newest durable review; never fall back past a refresh."""
    row, route = _application_route(store, profile_id, application_id)
    if route.get("provider") != "workday":
        return None
    frozen = _frozen_review_input(store, profile_id, application_id, row, route)
    current_input_fingerprint = _input_fingerprint(frozen)
    from workers.application_form_review import FormReviewError, _existing_result

    with store.engine.connect() as connection:
        latest_order = [
            m.operations.c.params["admitted_at"].as_string().desc().nullslast(),
            m.operations.c.updated_at.desc(),
            m.operations.c.id.desc(),
        ]
        operations = list(connection.execute(select(m.operations).where(
            m.operations.c.kind == _FORM_REVIEW_KIND,
            m.operations.c.entity_id == application_id,
            m.operations.c.params["profile_id"].as_string() == profile_id,
        ).order_by(*latest_order).limit(1)).mappings())
        if not operations:
            return {"state": "IDLE", "analysis": None, "input_current": True}
        operation = operations[0]
        params = operation["params"] if isinstance(operation["params"], Mapping) else {}
        receipt_count = connection.execute(select(func.count()).select_from(m.audit).where(
            m.audit.c.entity_id == application_id,
            m.audit.c.action == _FORM_REVIEW_KIND,
            m.audit.c.payload["operation_id"].as_string() == operation["id"],
        )).scalar_one()
        if receipt_count > 1:
            raise HTTPException(409, {"code": "DYNAMIC_FIELDS_STALE"})
        try:
            receipt = _existing_result(
                connection, application_id, operation["id"],
                params.get("request_id"), params.get("input_fingerprint"),
            )
        except FormReviewError:
            raise HTTPException(409, {"code": "DYNAMIC_FIELDS_STALE"}) from None
        analysis = receipt.get("analysis") if receipt is not None else None
    return {
        "state": operation["state"],
        "analysis": analysis,
        "input_current": params.get("input_fingerprint") == current_input_fingerprint,
    }


def _workday_dynamic_fields(envelope: Mapping) -> list[dict] | None:
    """Adapt the validated value-free envelope to the existing public DTO."""
    fields = envelope.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > _MAX_FIELDS:
        return None
    projected = []
    for item in fields:
        if not isinstance(item, Mapping):
            return None
        field_id = item.get("id") or item.get("name")
        field_type = _workday_field_type(item.get("type"))
        required = item.get("required")
        conditional = item.get("conditional")
        conditional_on = item.get("conditional_on")
        repeatable = item.get("repeatable")
        # The strict envelope contains structural identity only. It carries no
        # answer values, labels, options, or authorization to fill a field.
        if (not isinstance(field_id, str) or not _IDENTIFIER.fullmatch(field_id)
                or field_type is None or type(required) is not bool
                or (conditional is not None and type(conditional) is not bool)
                or (conditional_on is not None and (
                    not isinstance(conditional_on, str)
                    or not _IDENTIFIER.fullmatch(conditional_on)))
                or (repeatable is not None and type(repeatable) is not bool)):
            return None
        projected.append({
            "id": field_id,
            "label": "Workday question requires manual review",
            "type": field_type,
            "required": required,
            "visible": True,
            "depends_on": conditional_on,
            "options": [],
            "repeatable": repeatable is True,
            "supported": False,
            "sensitive": True,
            "manual_review_required": True,
        })
    return projected


def create_dynamic_application_fields_router(store) -> APIRouter:
    """Expose current field metadata only; this router has no write operations."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/applications/{application_id}/dynamic-fields")
    def get_dynamic_application_fields(profile_id: str, application_id: str,
                                       expected_fingerprint: str | None = None):
        if expected_fingerprint is not None and not _FINGERPRINT.fullmatch(expected_fingerprint):
            raise HTTPException(422, {"code": "INVALID_EXPECTED_FINGERPRINT"})
        try:
            application = store.application(application_id)
            if application.get("profile_id") != profile_id:
                raise NoResultFound("Application is not owned by this profile")
            current_profile = store.profile(profile_id)
        except NoResultFound:
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"}) from None

        with store.engine.connect() as connection:
            provider = connection.execute(select(m.routes.c.provider).where(
                m.routes.c.id == application.get("route_id"),
            )).scalar_one_or_none()
        if provider == "workday":
            current = _latest_workday_dynamic_review(
                store, profile_id, application_id, application)
            if not current["input_current"]:
                raise HTTPException(409, {"code": "DYNAMIC_FIELDS_STALE"})
            if current["state"] != "COMPLETED" or not isinstance(current["analysis"], Mapping):
                if expected_fingerprint is not None:
                    raise HTTPException(409, {"code": "DYNAMIC_FIELDS_STALE"})
                return {
                    "profile_id": profile_id,
                    "application_id": application_id,
                    "fingerprint": None,
                    "status": "REVIEW_REQUIRED",
                    "fields": [],
                }
            dynamic_review = current["analysis"].get("dynamic_review")
            from services.applications.workday_dynamic_review import (
                validate_workday_dynamic_review,
            )
            envelope = validate_workday_dynamic_review(dynamic_review)
            fields = _workday_dynamic_fields(envelope) if envelope is not None else None
            if fields is None:
                if expected_fingerprint is not None:
                    raise HTTPException(409, {"code": "DYNAMIC_FIELDS_STALE"})
                return {
                    "profile_id": profile_id,
                    "application_id": application_id,
                    "fingerprint": None,
                    "status": "REVIEW_REQUIRED",
                    "fields": [],
                }
            fingerprint = envelope["fingerprint"]
            if expected_fingerprint is not None and expected_fingerprint != fingerprint:
                raise HTTPException(409, {"code": "DYNAMIC_FIELDS_STALE"})
            return {
                "profile_id": profile_id,
                "application_id": application_id,
                "fingerprint": fingerprint,
                "status": "REVIEW_REQUIRED",
                "fields": fields,
                "optional_sections": [],
                "repeatable_groups": [],
            }

        report = latest_preflight(store, application_id)
        if not isinstance(report, Mapping):
            return {
                "profile_id": profile_id,
                "application_id": application_id,
                "fingerprint": None,
                "status": "REVIEW_REQUIRED",
                "fields": [],
            }

        snapshot = _snapshot(report)
        fingerprint = snapshot["metadata_fingerprint"]
        if (not isinstance(fingerprint, str)
                or not _FINGERPRINT.fullmatch(fingerprint)
                or snapshot["profile_version"] != application.get("profile_version")
                or snapshot["document_version"] != application.get("document_version")
                or snapshot["route_id"] != application.get("route_id")
                or current_profile.get("version") != application.get("profile_version")):
            raise HTTPException(409, {"code": "DYNAMIC_FIELDS_STALE"})
        fields = _fields(report)
        if fields is None or report.get("result") not in {"PASS", "FAIL", "UNKNOWN"}:
            return {
                "profile_id": profile_id,
                "application_id": application_id,
                "fingerprint": None,
                "status": "REVIEW_REQUIRED",
                "fields": [],
            }
        optional_sections = _sections(report, fields)
        repeatable_groups = _repeatable_groups(fields)
        if optional_sections is None or repeatable_groups is None:
            return {
                "profile_id": profile_id,
                "application_id": application_id,
                "fingerprint": None,
                "status": "REVIEW_REQUIRED",
                "fields": [],
            }
        current_fingerprint = _field_fingerprint(
            profile_id, application_id, application, snapshot, fields,
            optional_sections, repeatable_groups)
        if expected_fingerprint is not None and expected_fingerprint != current_fingerprint:
            raise HTTPException(409, {"code": "DYNAMIC_FIELDS_STALE"})
        body = {
            "profile_id": profile_id,
            "application_id": application_id,
            "fingerprint": current_fingerprint,
            "fields": fields,
            "optional_sections": optional_sections,
            "repeatable_groups": repeatable_groups,
        }
        if report.get("result") != "PASS":
            body["status"] = "REVIEW_REQUIRED"
        return body

    return router
