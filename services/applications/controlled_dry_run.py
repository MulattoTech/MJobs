"""Explicit local ATS fixture preparation; never employer interaction or submission.

Only a trusted product template reaches the browser. Values and document bytes
exist transiently; durable receipts contain identities, sizes and hashes only.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.applications.form_review_providers import normalize_application_route
from services.applications.rehearsal import build_rehearsal, RehearsalError
from services.applications.rehearsal_store import get_application_rehearsal
from services.applications.supporting_documents import read_selected_files, list_selection
from services.applications.upload_integrity import (
    attach_verified_files, snapshot_prepared, snapshot_supporting, MAX_TOTAL_BYTES,
)
from workers.dispatch import enqueue_operation
from workers.operations import OperationCancelled, OperationTimedOut

KIND = "APPLICATION_DRY_RUN"
CONTEXT = "CONTROLLED_FIXTURE"
ORIGIN = "http://127.0.0.1:1/mjobs-controlled-application"


class ControlledDryRunError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def template(provider):
    if provider not in {"greenhouse", "lever"}:
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_ROUTE_UNSUPPORTED")
    # No scripts, external resources, form action, or submit control. This is a
    # versioned product fixture, not a reconstruction of a public ATS form.
    markup = f'''<!doctype html><meta charset="utf-8"><title>{provider} local fixture v1</title>
    <form><input name="full_name" required><input name="email" type="email" required>
    <textarea name="weekend_availability" required></textarea>
    <input name="resume" type="file" accept="application/pdf" required>
    <input name="cover_letter" type="file" accept="application/pdf" required>
    <input name="supporting_documents" type="file" multiple></form>'''
    fields = [("full_name", "text", True), ("email", "email", True),
              ("weekend_availability", "textarea", True), ("resume", "file", True),
              ("cover_letter", "file", True), ("supporting_documents", "file", False)]
    return {
        "html": markup, "template_id": f"{provider}-local-v1",
        "template_sha256": hashlib.sha256(markup.encode()).hexdigest(),
        "analysis": {"status": "ANALYZED", "reason": None, "fields": [
            {"id": None, "name": name, "type": kind, "required": required,
             "review_reason": None} for name, kind, required in fields]},
    }


def preview(store, profile_id, application_id):
    with store.engine.connect() as connection:
        route = connection.execute(select(m.routes.c.provider, m.routes.c.url)
            .select_from(m.applications.join(m.routes, m.applications.c.route_id == m.routes.c.id))
            .where(m.applications.c.id == application_id,
                   m.applications.c.profile_id == profile_id)).mappings().one_or_none()
    if route is None:
        raise ControlledDryRunError("APPLICATION_NOT_FOUND")
    if normalize_application_route(route["provider"], route["url"]) is None:
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_ROUTE_UNSUPPORTED")
    fixture = template(route["provider"])
    result = build_rehearsal(store, profile_id, application_id, _controlled=fixture)
    selection = list_selection(store, profile_id, application_id, _controlled_fixture=True)
    if selection["unavailable_revision_ids"]:
        result["ready"] = False
        result["blockers"].append("SUPPORTING_DOCUMENT_UNAVAILABLE")
    return {**result, "context": CONTEXT, "provider": route["provider"],
            "template_id": fixture["template_id"], "template_sha256": fixture["template_sha256"]}


def reviewed_preview(store, profile_id, application_id):
    return get_application_rehearsal(store, profile_id, application_id, preview_builder=preview)


def require_scope(store, profile_id, application_id):
    with store.engine.connect() as connection:
        if connection.execute(select(m.applications.c.id).where(
                m.applications.c.id == application_id,
                m.applications.c.profile_id == profile_id)).first() is None:
            raise ControlledDryRunError("APPLICATION_NOT_FOUND")


def lock_scope(connection, profile_id, application_id):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    app = connection.execute(select(m.applications.c.route_id).where(
        m.applications.c.id == application_id, m.applications.c.profile_id == profile_id,
    ).with_for_update()).first()
    if app is None:
        raise ControlledDryRunError("APPLICATION_NOT_FOUND")
    connection.execute(select(m.profiles.c.id).where(m.profiles.c.id == profile_id)
                       .with_for_update()).first()
    connection.execute(select(m.routes.c.id).where(m.routes.c.id == app.route_id)
                       .with_for_update()).first()


def _current(store, profile_id, application_id, fingerprint):
    result = reviewed_preview(store, profile_id, application_id)
    if result["fingerprint"] != fingerprint:
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_STALE")
    if not result["ready"] or result["latest_review"] is None:
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_REVIEW_REQUIRED")
    return result


def _run_filters(profile_id, application_id):
    return (m.operations.c.kind == KIND, m.operations.c.entity_id == application_id,
            m.operations.c.params['profile_id'].as_string() == profile_id)


def queue(store, profile_id, application_id, request_id, fingerprint, *, retry_of=None):
    with store.engine.begin() as connection:
        lock_scope(connection, profile_id, application_id)
        current = _current(store, profile_id, application_id, fingerprint)
        params = {key: current[key] for key in (
            "profile_id", "application_id", "profile_version", "route_id", "fingerprint",
            "context", "provider", "template_id", "template_sha256",
            "documents", "supporting_documents",
        )}
        params["request_id"] = request_id
        if retry_of is not None:
            params['retry_of'] = retry_of
        rows = list(connection.execute(select(m.operations).where(
            m.operations.c.kind == KIND, m.operations.c.entity_id == application_id,
            m.operations.c.params["profile_id"].as_string() == profile_id,
            m.operations.c.params["request_id"].as_string() == request_id,
        )).mappings())
        if rows:
            if len(rows) != 1 or rows[0]["params"] != params:
                raise ControlledDryRunError("CONTROLLED_DRY_RUN_REQUEST_ID_REUSED")
            operation_id, state = rows[0]["id"], rows[0]["state"]
        else:
            prior_runs = list(connection.execute(select(m.operations).where(
                *_run_filters(profile_id, application_id))).mappings())
            if any(row['state'] in {'QUEUED', 'RUNNING'} for row in prior_runs):
                raise ControlledDryRunError('CONTROLLED_DRY_RUN_ACTIVE')
            if retry_of is not None:
                previous = [row for row in prior_runs if row['params'].get('request_id') == retry_of]
                if len(previous) != 1 or request_id == retry_of:
                    raise ControlledDryRunError('CONTROLLED_DRY_RUN_RETRY_INVALID')
                if previous[0]['state'] not in {'COMPLETED', 'CANCELLED', 'FAILED', 'BLOCKED', 'DEAD_LETTERED'}:
                    raise ControlledDryRunError('CONTROLLED_DRY_RUN_RETRY_INVALID')
            elif any(row['params'].get('fingerprint') == fingerprint for row in prior_runs):
                raise ControlledDryRunError('CONTROLLED_DRY_RUN_RETRY_REVIEW_REQUIRED')
            operation_id = enqueue_operation(store, KIND, application_id, params, connection=connection)
            state = "QUEUED"
    return {"operation_id": operation_id, "request_id": request_id, "state": state}


def cancel(store, profile_id, application_id, request_id):
    """Resolve an immutable owner-scoped intent before invoking the shared fence."""
    require_scope(store, profile_id, application_id)
    current = read_run(store, profile_id, application_id, request_id)
    if current['request_id'] is None:
        raise ControlledDryRunError('CONTROLLED_DRY_RUN_NOT_FOUND')
    from workers.operations import request_cancel
    request_cancel(store, current['operation_id'])
    return read_run(store, profile_id, application_id, request_id)


def read_run(store, profile_id, application_id, request_id=None):
    filters = [m.operations.c.kind == KIND, m.operations.c.entity_id == application_id,
               m.operations.c.params["profile_id"].as_string() == profile_id]
    if request_id is not None:
        filters.append(m.operations.c.params["request_id"].as_string() == request_id)
    with store.engine.connect() as connection:
        rows = list(connection.execute(select(m.operations).where(*filters)
            .order_by(m.operations.c.updated_at.desc(), m.operations.c.id.desc())
            .limit(2 if request_id is not None else 1)).mappings())
        if request_id is not None and len(rows) > 1:
            raise ControlledDryRunError("CONTROLLED_DRY_RUN_REQUEST_ID_REUSED")
        if not rows:
            return {"operation_id": None, "request_id": None, "state": "IDLE", "receipt": None,
                    "cancel_requested": False, "retry_of": None, "error_code": None}
        row = rows[0]
        receipt = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.id == identity(KIND, row["id"]), m.audit.c.action == KIND,
        )).scalar_one_or_none()
    return {"operation_id": row['id'], "request_id": row["params"]["request_id"], "state": row["state"],
            "fingerprint": row['params']['fingerprint'],
            "cancel_requested": row['cancel_requested'], "retry_of": row['params'].get('retry_of'),
            "receipt": receipt, "error_code": row["error"] if row["error"] in {
                "CONTROLLED_DRY_RUN_STALE", "CONTROLLED_DRY_RUN_REVIEW_REQUIRED",
                "CONTROLLED_DRY_RUN_DOCUMENT_CHANGED", "CONTROLLED_DRY_RUN_BROWSER_FAILED",
                "CANCEL_REQUESTED", "OPERATION_TIMEOUT",
            } else None}


def _snapshots(store, output_dir, current):
    profile_id, application_id = current["profile_id"], current["application_id"]
    root = Path(output_dir).resolve()
    application_root = (root / application_id).resolve()
    if not application_root.is_relative_to(root):
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_DOCUMENT_CHANGED")
    result = []
    with store.engine.connect() as connection:
        for kind in ("resume", "cover-letter"):
            expected = next((row for row in current["documents"] if row["kind"] == kind), None)
            if expected is None:
                raise ControlledDryRunError("CONTROLLED_DRY_RUN_DOCUMENT_CHANGED")
            row = connection.execute(select(m.documents).where(
                m.documents.c.id == expected["id"],
                m.documents.c.application_id == application_id,
                m.documents.c.profile_version == current["profile_version"],
                m.documents.c.kind == kind,
            )).mappings().one_or_none()
            if row is None or not isinstance(row["files"], dict) or not isinstance(row["files"].get("pdf"), str):
                raise ControlledDryRunError("CONTROLLED_DRY_RUN_DOCUMENT_CHANGED")
            path = (application_root / row["files"]["pdf"]).resolve()
            if not path.is_relative_to(application_root):
                raise ControlledDryRunError("CONTROLLED_DRY_RUN_DOCUMENT_CHANGED")
            result.append(snapshot_prepared(row, path))
    supporting = [snapshot_supporting(row) for row in read_selected_files(
        store, output_dir, profile_id, application_id, _controlled_fixture=True)]
    if (len(supporting) > 5 or
            sum(len(item.content) for item in result+supporting) > MAX_TOTAL_BYTES):
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_DOCUMENT_CHANGED")
    return result, supporting


def _budget_ms(check, maximum=5000):
    check()
    remaining = getattr(check, "remaining_seconds", lambda: maximum / 1000)()
    if remaining <= 0:
        raise OperationTimedOut("Controlled fixture operation deadline expired")
    return max(1, min(maximum, int(remaining * 1000)))


def run_browser(current, prepared, supporting, *, check):
    from playwright.sync_api import sync_playwright
    fixture = template(current["provider"])
    if fixture["template_sha256"] != current["template_sha256"]:
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_STALE")
    blocked = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=_budget_ms(check, 15000))
        try:
            context = browser.new_context(service_workers="block")
            def local_only(route):
                request = route.request
                if request.url == ORIGIN and request.method == "GET" and request.is_navigation_request():
                    route.fulfill(status=200, content_type="text/html", body=fixture["html"],
                        headers={"Content-Security-Policy": "default-src 'none'; form-action 'none'; connect-src 'none'"})
                else:
                    blocked.append(True)
                    route.abort()
            context.route("**/*", local_only)
            page = context.new_page()
            page.set_default_timeout(_budget_ms(check))
            page.set_default_navigation_timeout(_budget_ms(check))
            check()
            page.goto(ORIGIN, wait_until="domcontentloaded", timeout=_budget_ms(check))
            if page.locator("form").count() != 1 or page.locator("form input, form textarea, form select").count() != len(current["fields"]):
                raise ControlledDryRunError("CONTROLLED_DRY_RUN_REVIEW_REQUIRED")
            for field in current["fields"]:
                page.set_default_timeout(_budget_ms(check))
                if field["source"] not in {"CONTACT", "VERIFIED_ANSWER", "DOCUMENT"}:
                    raise ControlledDryRunError("CONTROLLED_DRY_RUN_REVIEW_REQUIRED")
                control = page.locator('[name="'+field["key"]+'"]')
                if control.count() != 1:
                    raise ControlledDryRunError("CONTROLLED_DRY_RUN_REVIEW_REQUIRED")
                metadata = control.evaluate("node => ({name:node.name, type:node.type, required:node.required})")
                if metadata != {"name": field["key"], "type": field["type"], "required": field["required"]}:
                    raise ControlledDryRunError("CONTROLLED_DRY_RUN_REVIEW_REQUIRED")
                if field["source"] != "DOCUMENT":
                    control.fill(field["value"])
                    if control.input_value() != field["value"]:
                        raise ControlledDryRunError("CONTROLLED_DRY_RUN_REVIEW_REQUIRED")
            if not page.locator("form").evaluate("form => Array.from(form.elements).filter(e => e.type !== 'file').every(e => e.checkValidity())"):
                raise ControlledDryRunError("CONTROLLED_DRY_RUN_REVIEW_REQUIRED")
            manifest = []
            for snapshot, field in zip(prepared, ("resume", "cover_letter")):
                page.set_default_timeout(_budget_ms(check))
                manifest.extend(attach_verified_files(page.locator('[name="'+field+'"]'), [snapshot], check=check))
            if supporting:
                page.set_default_timeout(_budget_ms(check))
                manifest.extend(attach_verified_files(page.locator('[name="supporting_documents"]'), supporting, check=check))
            check()
            if blocked or not page.locator("form").evaluate("form => form.checkValidity()"):
                raise ControlledDryRunError("CONTROLLED_DRY_RUN_REVIEW_REQUIRED")
            return manifest
        finally:
            browser.close()


def execute(store, output_dir, operation, *, check):
    params = operation["params"]
    if (operation["kind"] != KIND or params.get("context") != CONTEXT
            or operation["entity_id"] != params.get("application_id")):
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_STALE")
    receipt_id = identity(KIND, operation["id"])
    with store.engine.connect() as connection:
        receipt = connection.execute(select(m.audit.c.payload).where(m.audit.c.id == receipt_id)).scalar_one_or_none()
    if receipt is not None:
        return receipt
    try:
        current = _current(store, params["profile_id"], params["application_id"], params["fingerprint"])
        if any(params.get(key) != current.get(key) for key in (
                "profile_version", "route_id", "provider", "template_id", "template_sha256",
                "documents", "supporting_documents")):
            raise ControlledDryRunError("CONTROLLED_DRY_RUN_STALE")
        prepared, supporting = _snapshots(store, output_dir, current)
        manifest = run_browser(current, prepared, supporting, check=check)
    except (ControlledDryRunError, OperationCancelled, OperationTimedOut):
        raise
    except RehearsalError:
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_STALE") from None
    except ValueError:
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_DOCUMENT_CHANGED") from None
    except Exception:
        check()
        raise ControlledDryRunError("CONTROLLED_DRY_RUN_BROWSER_FAILED") from None
    check()
    payload = {"operation_id": operation["id"], "profile_id": params["profile_id"],
               "application_id": params["application_id"], "fingerprint": params["fingerprint"],
               "context": CONTEXT, "template_id": current["template_id"],
               "template_sha256": current["template_sha256"],
               "verification": "BROWSER_SELECTED_BYTES", "documents": manifest,
               "provider_acceptance_claimed": False, "submission_enabled": False}
    with store.engine.begin() as connection:
        lock_scope(connection, params["profile_id"], params["application_id"])
        _current(store, params["profile_id"], params["application_id"], params["fingerprint"])
        row = connection.execute(select(m.operations).where(m.operations.c.id == operation["id"])
                                 .with_for_update()).mappings().one()
        if (row["state"] != "RUNNING" or row["kind"] != KIND or row["params"] != params
                or row["entity_id"] != operation["entity_id"]
                or row["attempt_count"] != operation["attempt_count"]
                or row["cancel_requested"] or not row["lease_expires_at"]
                or row["lease_expires_at"] <= now()):
            raise OperationCancelled("Controlled dry-run operation ownership expired")
        prior = connection.execute(select(m.audit.c.payload).where(m.audit.c.id == receipt_id)).scalar_one_or_none()
        if prior is not None:
            return prior
        connection.execute(m.audit.insert().values(id=receipt_id, entity_id=params["application_id"],
                           action=KIND, timestamp=now(), payload=payload))
    return payload
