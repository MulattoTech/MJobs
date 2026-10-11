"""Durable, read-only form inspection for supported public application routes."""
from __future__ import annotations

import hashlib
import json
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity
from services.applications.form_review_providers import normalize_application_route
from workers.dispatch import enqueue_operation


_KIND = "APPLICATION_FORM_REVIEW"
_SAFE_ERROR = re.compile(r"FORM_REVIEW_[A-Z0-9_]{1,70}\Z")


class FormReviewRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _application_route(store, profile_id: str, application_id: str):
    with store.engine.connect() as connection:
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
    if row["provider"] not in {"greenhouse", "lever"} or row["target"] == "MOCK":
        raise HTTPException(409, {"code": "FORM_REVIEW_ROUTE_UNSUPPORTED"})
    normalized = normalize_application_route(row["provider"], row["url"])
    if normalized is None:
        raise HTTPException(409, {"code": "FORM_REVIEW_ROUTE_UNSUPPORTED"})
    return dict(row), normalized


def create_application_form_review_router(store) -> APIRouter:
    router = APIRouter()

    def review_response(profile_id: str, application_id: str, request_id: str | None):
        filters = [
            m.operations.c.kind == _KIND,
            m.operations.c.entity_id == application_id,
            m.operations.c.params["profile_id"].as_string() == profile_id,
        ]
        if request_id is not None:
            filters.append(m.operations.c.params["request_id"].as_string() == request_id)
        with store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                *filters,
            ).order_by(m.operations.c.updated_at.desc(), m.operations.c.id.desc()).limit(1)).mappings().one_or_none()
            if row is None:
                return {
                    "request_id": None,
                    "state": "IDLE",
                    "error_code": None,
                    "analysis": None,
                }
            event_rows = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == application_id,
                m.audit.c.action == _KIND,
            )).mappings()
            report = next((event["payload"].get("analysis") for event in event_rows
                           if isinstance(event["payload"], dict)
                           and event["payload"].get("operation_id") == row["id"]), None)
        params = row["params"] if isinstance(row["params"], dict) else {}
        error = row["error"] if isinstance(row["error"], str) and _SAFE_ERROR.fullmatch(row["error"]) else None
        return {
            "request_id": params.get("request_id"),
            "state": row["state"],
            "error_code": error,
            "analysis": report,
        }

    @router.post(
        "/profiles/{profile_id}/applications/{application_id}/form-reviews",
        status_code=202,
    )
    def queue_form_review(profile_id: str, application_id: str, value: FormReviewRequest):
        row, route = _application_route(store, profile_id, application_id)
        profile = store.profile(profile_id)
        frozen = {
            "profile_id": profile_id,
            "application_id": application_id,
            "route_id": row["route_id"],
            "route_url": route["url"],
            "provider": route["provider"],
            "profile_version": str(profile["version"]),
        }
        input_fingerprint = hashlib.sha256(json.dumps(
            frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest()
        params = {**frozen, "request_id": value.request_id,
                  "input_fingerprint": input_fingerprint}
        operation_id = enqueue_operation(store, _KIND, application_id, params)
        with store.engine.connect() as connection:
            state = connection.execute(select(m.operations.c.state).where(
                m.operations.c.id == operation_id,
            )).scalar_one()
        return {"request_id": value.request_id, "operation_id": operation_id, "state": state}

    @router.get(
        "/profiles/{profile_id}/applications/{application_id}/form-reviews/latest",
    )
    def read_latest_form_review(profile_id: str, application_id: str):
        row, _ = _application_route(store, profile_id, application_id)
        return review_response(profile_id, application_id, None)

    @router.get(
        "/profiles/{profile_id}/applications/{application_id}/form-reviews/{request_id}",
    )
    def read_form_review(profile_id: str, application_id: str, request_id: str):
        response = review_response(profile_id, application_id, request_id)
        if response["request_id"] is None:
            raise HTTPException(404, "Form review request not found")
        return response

    return router
