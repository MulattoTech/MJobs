"""Durable, read-only Greenhouse application form review handler."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from collections.abc import Mapping

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import now
from services.applications.form_review_providers import (
    analyze_application_form,
    normalize_application_route,
)
from services.applications.workday_dynamic_review import validate_workday_dynamic_review
from workers.operations import OperationCancelled, OperationTimedOut


_KIND = "APPLICATION_FORM_REVIEW"
_AUDIT_ACTION = "APPLICATION_FORM_REVIEW"
_MAX_HTML_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:\-\[\]]{0,99}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_FIELD_TYPES = {
    "text", "email", "tel", "url", "number", "date", "datetime-local",
    "time", "month", "week", "search", "password", "checkbox", "radio",
    "file", "select", "textarea", "other",
}
_REVIEW_REASONS = {
    "CREDENTIAL_FIELD", "SENSITIVE_FIELD", "AMBIGUOUS_FIELD",
    "UNSAFE_IDENTIFIER", "UNIDENTIFIABLE_FIELD", "UNSUPPORTED_FIELD_TYPE",
    "UNKNOWN_QUESTION",
}


class FormReviewError(ValueError):
    """A fixed, value-free worker error code."""

    def __init__(self, code: str):
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            code = "FORM_REVIEW_FAILED"
        self.code = code
        super().__init__(code)


def _params(operation):
    params = operation.get("params") if isinstance(operation, Mapping) else None
    if not isinstance(params, dict):
        raise FormReviewError("FORM_REVIEW_OPERATION_INVALID")
    for key in ("profile_id", "application_id", "route_id", "route_url",
                "provider", "request_id", "input_fingerprint"):
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise FormReviewError("FORM_REVIEW_OPERATION_INVALID")
    if (len(params["request_id"]) > 200
            or not _SHA256.fullmatch(params["input_fingerprint"])):
        raise FormReviewError("FORM_REVIEW_OPERATION_INVALID")
    return params


def _read_operation(connection, operation):
    operation_id = operation.get("id") if isinstance(operation, Mapping) else None
    if not isinstance(operation_id, str) or not operation_id:
        raise FormReviewError("FORM_REVIEW_OPERATION_INVALID")
    row = connection.execute(select(m.operations).where(
        m.operations.c.id == operation_id,
    ).with_for_update()).mappings().one_or_none()
    if (row is None or row["kind"] != _KIND
            or row["entity_id"] != operation.get("entity_id")
            or row["params"] != operation.get("params")
            or row["attempt_count"] != operation.get("attempt_count")):
        raise OperationCancelled("Form review operation ownership changed")
    return dict(row)


def _existing_result(connection, application_id, operation_id, request_id,
                     input_fingerprint):
    rows = connection.execute(select(m.audit.c.payload).where(
        m.audit.c.entity_id == application_id,
        m.audit.c.action == _AUDIT_ACTION,
    )).mappings()
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, dict) or payload.get("operation_id") != operation_id:
            continue
        if (payload.get("request_id") != request_id
                or payload.get("input_fingerprint") != input_fingerprint
                or not isinstance(payload.get("analysis"), dict)):
            raise FormReviewError("FORM_REVIEW_IDEMPOTENCY_CONFLICT")
        analysis = _safe_report(payload["analysis"])
        return {
            "operation_id": operation_id,
            "status": analysis["status"],
            "analysis": analysis,
        }
    return None


def _safe_report(report):
    if (not isinstance(report, dict)
            or set(report) not in ({"status", "reason", "fields"},
                                   {"status", "reason", "fields", "dynamic_review"})
            or not isinstance(report.get("status"), str)
            or report["status"] not in {"ANALYZED", "REVIEW_REQUIRED", "UNKNOWN"}
            or not isinstance(report.get("fields"), list)
            or len(report["fields"]) > 100):
        raise FormReviewError("FORM_REVIEW_ANALYSIS_INVALID")
    reason = report.get("reason")
    if reason is not None and (not isinstance(reason, str) or not _CODE.fullmatch(reason)):
        raise FormReviewError("FORM_REVIEW_ANALYSIS_INVALID")
    safe_fields = []
    for field in report["fields"]:
        if not isinstance(field, dict) or set(field) != {
            "id", "name", "type", "required", "review_reason",
        }:
            raise FormReviewError("FORM_REVIEW_ANALYSIS_INVALID")
        for key in ("id", "name"):
            value = field[key]
            if (value is not None and
                    (not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value))):
                raise FormReviewError("FORM_REVIEW_ANALYSIS_INVALID")
        if (not isinstance(field["type"], str) or field["type"] not in _FIELD_TYPES
                or type(field["required"]) is not bool):
            raise FormReviewError("FORM_REVIEW_ANALYSIS_INVALID")
        review_reason = field["review_reason"]
        if review_reason is not None and (not isinstance(review_reason, str)
                                           or review_reason not in _REVIEW_REASONS):
            raise FormReviewError("FORM_REVIEW_ANALYSIS_INVALID")
        safe_fields.append({
            "id": field["id"], "name": field["name"],
            "type": field["type"], "required": field["required"],
            "review_reason": review_reason,
        })
    safe = {"status": report["status"], "reason": reason, "fields": safe_fields}
    if "dynamic_review" in report:
        dynamic = validate_workday_dynamic_review(report["dynamic_review"])
        if dynamic is None:
            raise FormReviewError("FORM_REVIEW_ANALYSIS_INVALID")
        safe["dynamic_review"] = dynamic
    return safe


def _validate_claimed(connection, operation):
    row = _read_operation(connection, operation)
    if row["state"] != "RUNNING" or row["cancel_requested"]:
        raise OperationCancelled("Form review operation is no longer active")
    lease = row.get("lease_expires_at")
    try:
        lease_at = datetime.fromisoformat(lease) if isinstance(lease, str) else None
    except ValueError:
        lease_at = None
    if lease_at is None:
        raise FormReviewError("FORM_REVIEW_OPERATION_INVALID")
    if lease_at.tzinfo is None:
        lease_at = lease_at.replace(tzinfo=timezone.utc)
    if lease_at <= datetime.now(timezone.utc):
        raise OperationTimedOut("Form review operation lease expired")
    return row


def _validate_application(connection, operation, params):
    if (operation.get("kind") != _KIND
            or operation.get("entity_id") != params["application_id"]):
        raise FormReviewError("FORM_REVIEW_INPUT_STALE")
    row = connection.execute(select(
        m.applications.c.id.label("application_id"),
        m.applications.c.profile_id,
        m.applications.c.route_id,
        m.routes.c.provider,
        m.routes.c.tenant,
        m.routes.c.requisition,
        m.routes.c.url,
    ).select_from(m.applications.join(
        m.routes, m.applications.c.route_id == m.routes.c.id,
    )).where(
        m.applications.c.id == params["application_id"],
    )).mappings().one_or_none()
    if row is None:
        raise FormReviewError("FORM_REVIEW_APPLICATION_NOT_FOUND")
    if (row["profile_id"] != params["profile_id"]
            or row["route_id"] != params["route_id"]
            or row["provider"] != params["provider"]):
        raise FormReviewError("FORM_REVIEW_INPUT_STALE")
    # Older metadata-only operations did not freeze a profile version. Preserve
    # their replay contract; every current API admission includes this version.
    if "profile_version" in params:
        version = connection.execute(select(m.profiles.c.version).where(
            m.profiles.c.id == params["profile_id"],
        ).with_for_update()).scalar_one_or_none()
        if version is None or str(version) != params["profile_version"]:
            raise FormReviewError("FORM_REVIEW_INPUT_STALE")
    normalized = normalize_application_route(
        row["provider"], row["url"], route_metadata=row,
    )
    if normalized is None:
        raise FormReviewError("FORM_REVIEW_ROUTE_UNSUPPORTED")
    if (row["provider"] == "workday"
            and (not isinstance(row["tenant"], str)
                 or row["tenant"].casefold() != normalized["tenant"]
                 or not isinstance(row["requisition"], str)
                 or row["requisition"].upper() != normalized["requisition"])):
        raise FormReviewError("FORM_REVIEW_ROUTE_UNSUPPORTED")
    if normalized["url"] != params["route_url"]:
        raise FormReviewError("FORM_REVIEW_INPUT_STALE")
    return normalized


def handle_form_review(store, operation, *, fetch_html, check):
    """Analyze one frozen supported application route and persist a safe report.

    `fetch_html` must be the caller's bounded, read-only public request function.
    `check` is the operation cancellation/deadline checkpoint; its exceptions
    intentionally propagate to the existing worker executor.
    """
    params = _params(operation)
    operation_id = operation.get("id") if isinstance(operation, Mapping) else None
    application_id = params["application_id"]
    if (operation.get("kind") != _KIND
            or operation.get("entity_id") != application_id):
        raise FormReviewError("FORM_REVIEW_INPUT_STALE")

    # A completed audit receipt survives a worker crash after commit and avoids
    # repeating the public read on operation replay.
    with store.engine.connect() as connection:
        _read_operation(connection, operation)
        replay = _existing_result(
            connection, application_id, operation_id,
            params["request_id"], params["input_fingerprint"],
        )
        if replay is not None:
            return replay
        _validate_claimed(connection, operation)
        normalized = _validate_application(connection, operation, params)

    check()
    try:
        html = fetch_html(normalized["url"])
    except (OperationCancelled, OperationTimedOut):
        raise
    except Exception:
        raise FormReviewError("FORM_REVIEW_FETCH_FAILED") from None
    check()
    if not isinstance(html, (str, bytes)):
        raise FormReviewError("FORM_REVIEW_FETCH_FAILED")
    try:
        if len(html.encode("utf-8") if isinstance(html, str) else html) > _MAX_HTML_BYTES:
            raise FormReviewError("FORM_REVIEW_HTML_TOO_LARGE")
    except UnicodeEncodeError:
        raise FormReviewError("FORM_REVIEW_FETCH_FAILED") from None

    try:
        analysis = _safe_report(analyze_application_form(
            params["provider"], html, route_url=normalized["url"],
        ))
    except FormReviewError:
        raise
    except Exception:
        raise FormReviewError("FORM_REVIEW_ANALYSIS_FAILED") from None
    check()

    payload = {
        "operation_id": operation_id,
        "request_id": params["request_id"],
        "input_fingerprint": params["input_fingerprint"],
        "profile_id": params["profile_id"],
        "route_id": params["route_id"],
        "provider": params["provider"],
        "analysis": analysis,
    }
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        _validate_claimed(connection, operation)
        replay = _existing_result(
            connection, application_id, operation_id,
            params["request_id"], params["input_fingerprint"],
        )
        if replay is not None:
            return replay
        _validate_application(connection, operation, params)
        store._audit(connection, application_id, _AUDIT_ACTION, payload)
    return {"operation_id": operation_id, "status": analysis["status"],
            "analysis": analysis}
