"""Durable, read-only form inspection for supported public application routes."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import now
from services.applications.form_review_providers import normalize_application_route
from services.applications.provider_registry import providers
from services.applications.manual_auth import (
    ManualAuthCompletionError, current_user_auth_completion,
)
from workers.dispatch import enqueue_operation
from workers.application_form_review import FormReviewError, _existing_result


_KIND = "APPLICATION_FORM_REVIEW"
_SAFE_ERROR = re.compile(r"FORM_REVIEW_[A-Z0-9_]{1,70}\Z")


class FormReviewRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _application_route(store, profile_id: str, application_id: str, *, connection=None):
    context = store.engine.connect() if connection is None else nullcontext(connection)
    with context as connection:
        row = connection.execute(select(
            m.applications.c.id.label("application_id"),
            m.applications.c.profile_id,
            m.applications.c.route_id,
            m.applications.c.target,
            m.routes.c.provider,
            m.routes.c.tenant,
            m.routes.c.requisition,
            m.routes.c.url,
        ).select_from(m.applications.join(
            m.routes, m.applications.c.route_id == m.routes.c.id,
        )).where(m.applications.c.id == application_id)).mappings().one_or_none()
    if row is None or row["profile_id"] != profile_id:
        raise HTTPException(404, "Application not found for this profile")
    if row["target"] == "MOCK":
        raise HTTPException(409, {"code": "FORM_REVIEW_ROUTE_UNSUPPORTED"})
    normalized = normalize_application_route(
        row["provider"], row["url"], route_metadata=row,
    )
    if normalized is None:
        raise HTTPException(409, {"code": "FORM_REVIEW_ROUTE_UNSUPPORTED"})
    return dict(row), normalized


def _frozen_review_input(store, profile_id: str, application_id: str, row, route,
                         *, profile_version=None):
    if profile_version is None:
        profile_version = store.profile(profile_id)["version"]
    return {
        "profile_id": profile_id,
        "application_id": application_id,
        "route_id": row["route_id"],
        "route_url": route["url"],
        "provider": route["provider"],
        "profile_version": str(profile_version),
    }


def _input_fingerprint(frozen):
    return hashlib.sha256(json.dumps(
        frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def create_application_form_review_router(store) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/profiles/{profile_id}/applications/{application_id}/form-review-capabilities",
    )
    def read_form_review_capabilities(profile_id: str, application_id: str):
        provider = None
        try:
            row, route = _application_route(store, profile_id, application_id)
            if row["target"] == "REAL" and route["provider"] in providers():
                provider = route["provider"]
        except HTTPException as error:
            if error.status_code != 409:
                raise
        # Rendering inspection controls never grants live interaction or submission.
        return {
            "schema_version": 1,
            "profile_id": profile_id,
            "application_id": application_id,
            "provider": provider,
            "review_supported": provider is not None,
            "read_only": True,
            "submission_supported": False,
        }

    def review_response(
        profile_id: str,
        application_id: str,
        request_id: str | None,
        current_input_fingerprint: str | None = None,
    ):
        filters = [
            m.operations.c.kind == _KIND,
            m.operations.c.entity_id == application_id,
            m.operations.c.params["profile_id"].as_string() == profile_id,
        ]
        if request_id is not None:
            filters.append(m.operations.c.params["request_id"].as_string() == request_id)
        with store.engine.connect() as connection:
            rows = list(connection.execute(select(m.operations).where(
                *filters,
            ).order_by(
                m.operations.c.params["admitted_at"].as_string().desc().nullslast(),
                m.operations.c.updated_at.desc(), m.operations.c.id.desc(),
            )
                .limit(2 if request_id is not None else 1)).mappings())
            if request_id is not None and len(rows) > 1:
                raise HTTPException(409, {"code": "FORM_REVIEW_IDEMPOTENCY_CONFLICT"})
            row = rows[0] if rows else None
            if row is None:
                return {
                    "request_id": None,
                    "state": "IDLE",
                    "error_code": None,
                    "analysis": None,
                }
            params = row["params"] if isinstance(row["params"], dict) else {}
            try:
                receipt = _existing_result(
                    connection, application_id, row["id"],
                    params.get("request_id"), params.get("input_fingerprint"),
                )
            except FormReviewError as error:
                raise HTTPException(409, {"code": error.code}) from None
            report = receipt["analysis"] if receipt else None
        params = row["params"] if isinstance(row["params"], dict) else {}
        if (current_input_fingerprint is not None
                and params.get("input_fingerprint") != current_input_fingerprint):
            return {
                "request_id": None,
                "state": "UNKNOWN",
                "error_code": None,
                "analysis": None,
            }
        error = row["error"] if isinstance(row["error"], str) and _SAFE_ERROR.fullmatch(row["error"]) else None
        response = {
            "request_id": params.get("request_id"),
            "state": row["state"],
            "error_code": error,
            "analysis": report,
        }
        if params.get("provider") == "workday":
            # Resolve revocation/expiry after closing the receipt connection:
            # the service owns its transaction. This declaration never supplies
            # a session or changes the persisted review/worker outcome.
            response["user_auth_declaration"] = None
            try:
                declaration = current_user_auth_completion(
                    store, profile_id, application_id, row["id"],
                )
            except ManualAuthCompletionError:
                declaration = None
            if (isinstance(declaration, dict)
                    and declaration.get("status") == "USER_DECLARED"
                    and declaration.get("review_operation_id") == row["id"]
                    and declaration.get("authenticated_session_available") is False
                    and all(isinstance(declaration.get(key), str)
                            for key in ("declared_at", "expires_at"))):
                response["user_auth_declaration"] = {
                    key: declaration[key] for key in (
                        "status", "review_operation_id", "declared_at", "expires_at",
                        "authenticated_session_available",
                    )
                }
        return response

    @router.post(
        "/profiles/{profile_id}/applications/{application_id}/form-reviews",
        status_code=202,
    )
    def queue_form_review(profile_id: str, application_id: str, value: FormReviewRequest):
        with store.engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            # Match rehearsal review's application -> profile -> route lock order.
            # Capture identity and reserve the request in this same transaction.
            application = connection.execute(select(m.applications.c.route_id).where(
                m.applications.c.id == application_id,
                m.applications.c.profile_id == profile_id,
            ).with_for_update()).first()
            if application is None:
                raise HTTPException(404, "Application not found for this profile")
            version = connection.execute(select(m.profiles.c.version).where(
                m.profiles.c.id == profile_id,
            ).with_for_update()).scalar_one()
            connection.execute(select(m.routes.c.id).where(
                m.routes.c.id == application.route_id,
            ).with_for_update()).first()
            row, route = _application_route(store, profile_id, application_id,
                                            connection=connection)
            frozen = _frozen_review_input(store, profile_id, application_id, row, route,
                                         profile_version=version)
            params = {**frozen, "request_id": value.request_id,
                      "input_fingerprint": _input_fingerprint(frozen)}
            existing = list(connection.execute(select(m.operations).where(
                m.operations.c.kind == _KIND,
                m.operations.c.entity_id == application_id,
                m.operations.c.params["profile_id"].as_string() == profile_id,
                m.operations.c.params["request_id"].as_string() == value.request_id,
            )).mappings())
            if existing:
                saved = {key: item for key, item in existing[0]["params"].items()
                         if key != "admitted_at"}
                if len(existing) != 1 or saved != params:
                    raise HTTPException(409, {"code": "FORM_REVIEW_IDEMPOTENCY_CONFLICT"})
                operation_id, state = existing[0]["id"], existing[0]["state"]
            else:
                # This metadata survives progress updates, retries and recovery.
                # Legacy rows retain their old ordering behind new admissions.
                params["admitted_at"] = now()
                operation_id = enqueue_operation(store, _KIND, application_id, params,
                                                 connection=connection)
                state = "QUEUED"
        return {"request_id": value.request_id, "operation_id": operation_id, "state": state}

    @router.get(
        "/profiles/{profile_id}/applications/{application_id}/form-reviews/latest",
    )
    def read_latest_form_review(profile_id: str, application_id: str):
        row, route = _application_route(store, profile_id, application_id)
        frozen = _frozen_review_input(store, profile_id, application_id, row, route)
        return review_response(
            profile_id,
            application_id,
            None,
            current_input_fingerprint=_input_fingerprint(frozen),
        )

    @router.get(
        "/profiles/{profile_id}/applications/{application_id}/form-reviews/requests/{request_id}",
    )
    @router.get(
        "/profiles/{profile_id}/applications/{application_id}/form-reviews/{request_id}",
    )
    def read_form_review(profile_id: str, application_id: str, request_id: str):
        response = review_response(profile_id, application_id, request_id)
        if response["request_id"] is None:
            raise HTTPException(404, "Form review request not found")
        return response

    return router
