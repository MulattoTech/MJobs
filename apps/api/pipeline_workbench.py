"""Profile-scoped read and safe preparation actions for the pipeline workbench."""
from __future__ import annotations

from collections.abc import Mapping
import re

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.operations.pipeline_actions import (
    action_fingerprint,
    bind_prepare_request,
    find_prepare_request,
    replay_prepare_request,
)


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STATES = frozenset({
    "QUEUED", "RUNNING", "COMPLETED", "FAILED", "BLOCKED", "CANCELLED",
    "DEAD_LETTERED", "DISCARDED",
})


class PipelinePrepareRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    expected_profile_version: str = Field(min_length=1, max_length=128)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def _read_workbench(store, profile_id: str) -> dict:
    from services.operations.pipeline_workbench import read_pipeline_workbench
    return read_pipeline_workbench(store, profile_id)


def _error(status: int, code: str):
    raise HTTPException(status_code=status, detail={"code": code})


def _safe_operation(value, *, request_id: str, job_id: str) -> dict:
    if (not isinstance(value, Mapping)
            or not isinstance(value.get("operation_id"), str)
            or not _ID.fullmatch(value["operation_id"])
            or not isinstance(value.get("state"), str)
            or value.get("state") not in _STATES):
        _error(503, "PIPELINE_PREPARE_UNAVAILABLE")
    return {
        "request_id": request_id,
        "job_id": job_id,
        "operation_id": value["operation_id"],
        "state": value["state"],
        "next_action": "REFRESH_WORKBENCH",
    }


def _snapshot_item(store, profile_id: str, job_id: str) -> tuple[dict, dict]:
    try:
        snapshot = _read_workbench(store, profile_id)
    except (LookupError, NoResultFound):
        _error(404, "PIPELINE_PROFILE_NOT_FOUND")
    except Exception:
        _error(503, "PIPELINE_WORKBENCH_UNAVAILABLE")
    if (not isinstance(snapshot, Mapping) or snapshot.get("profile_id") != profile_id
            or not isinstance(snapshot.get("profile_version"), str)
            or not isinstance(snapshot.get("items"), list)):
        _error(503, "PIPELINE_WORKBENCH_UNAVAILABLE")
    matches = [item for item in snapshot["items"]
               if isinstance(item, Mapping) and item.get("job_id") == job_id]
    if len(matches) != 1:
        _error(404, "PIPELINE_JOB_NOT_FOUND")
    return dict(snapshot), dict(matches[0])


def create_pipeline_workbench_router(store, prepare_callback) -> APIRouter:
    """Build read/action routes; preparation policy remains in the injected callback."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/pipeline-workbench")
    def pipeline_workbench_read(profile_id: str):
        if not _ID.fullmatch(profile_id):
            _error(404, "PIPELINE_PROFILE_NOT_FOUND")
        try:
            result = _read_workbench(store, profile_id)
        except HTTPException:
            raise
        except (LookupError, NoResultFound):
            _error(404, "PIPELINE_PROFILE_NOT_FOUND")
        except Exception:
            _error(503, "PIPELINE_WORKBENCH_UNAVAILABLE")
        if not isinstance(result, Mapping) or result.get("profile_id") != profile_id:
            _error(503, "PIPELINE_WORKBENCH_UNAVAILABLE")
        return dict(result)

    @router.post(
        "/profiles/{profile_id}/pipeline-workbench/{job_id}/prepare",
        status_code=202,
    )
    def pipeline_workbench_prepare(
        profile_id: str,
        job_id: str,
        value: PipelinePrepareRequest = Body(...),
    ):
        if not _ID.fullmatch(profile_id) or not _ID.fullmatch(job_id):
            _error(404, "PIPELINE_JOB_NOT_FOUND")
        if value.request_id != value.request_id.strip():
            _error(422, "PIPELINE_PREPARE_INVALID_REQUEST")

        request_fingerprint = action_fingerprint(
            profile_id=profile_id,
            job_id=job_id,
            request_id=value.request_id,
            expected_profile_version=value.expected_profile_version,
            readiness_fingerprint=value.fingerprint,
        )
        try:
            with store.engine.begin() as connection:
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                profile_statement = select(m.profiles.c.id).where(
                    m.profiles.c.id == profile_id,
                )
                if connection.dialect.name == "postgresql":
                    profile_statement = profile_statement.with_for_update()
                if connection.execute(profile_statement).scalar_one_or_none() is None:
                    _error(404, "PIPELINE_PROFILE_NOT_FOUND")

                existing = find_prepare_request(
                    store, profile_id, value.request_id, connection=connection,
                )
                if existing is not None:
                    try:
                        replay = replay_prepare_request(existing, request_fingerprint)
                    except ValueError as exc:
                        code = str(exc)
                        _error(409, code if code in {
                            "PIPELINE_PREPARE_REQUEST_CONFLICT",
                            "PIPELINE_PREPARE_STATE_INVALID",
                        } else "PIPELINE_PREPARE_REQUEST_CONFLICT")
                    return _safe_operation(
                        replay, request_id=value.request_id, job_id=job_id,
                    )

                snapshot, item = _snapshot_item(store, profile_id, job_id)
                if (value.expected_profile_version != snapshot["profile_version"]
                        or value.fingerprint != item.get("fingerprint")):
                    _error(409, "PIPELINE_READINESS_STALE")
                if item.get("can_prepare") is not True:
                    _error(409, "PIPELINE_PREPARE_NOT_READY")
                route_mode = item.get("route_mode")
                route_id = item.get("route_id")
                if (not isinstance(route_mode, str)
                        or route_mode not in {"DEFAULT", "THIS_ROUTE"}
                        or not isinstance(route_id, str)
                        or not _ID.fullmatch(route_id)):
                    _error(409, "PIPELINE_PREPARE_NOT_READY")

                callback_values = {
                    "profile_id": profile_id,
                    "job_id": job_id,
                    "request_id": value.request_id,
                    "expected_profile_version": value.expected_profile_version,
                    "route_mode": "THIS_ROUTE",
                    "route_id": route_id,
                }
                try:
                    result = prepare_callback(callback_values, connection)
                except HTTPException as exc:
                    detail = exc.detail
                    code = detail.get("code") if isinstance(detail, dict) else None
                    if not isinstance(code, str) or not re.fullmatch(
                            r"[A-Z][A-Z0-9_]{0,63}", code):
                        code = "PIPELINE_PREPARE_VALIDATION_FAILED"
                    _error(exc.status_code if 400 <= exc.status_code < 500 else 503, code)
                except Exception:
                    _error(503, "PIPELINE_PREPARE_UNAVAILABLE")

                operation = _safe_operation(
                    result, request_id=value.request_id, job_id=job_id,
                )
                try:
                    persisted = bind_prepare_request(
                        store,
                        operation["operation_id"],
                        request_fingerprint,
                        profile_id=profile_id,
                        job_id=job_id,
                        request_id=value.request_id,
                        expected_profile_version=value.expected_profile_version,
                        connection=connection,
                    )
                except ValueError as exc:
                    code = str(exc)
                    _error(409 if code == "PIPELINE_PREPARE_REQUEST_CONFLICT" else 503,
                           code if code in {
                               "PIPELINE_PREPARE_REQUEST_CONFLICT",
                               "PIPELINE_PREPARE_OPERATION_NOT_FOUND",
                               "PIPELINE_PREPARE_OPERATION_SCOPE_MISMATCH",
                           } else "PIPELINE_PREPARE_UNAVAILABLE")
                return _safe_operation(persisted, request_id=value.request_id, job_id=job_id)
        except HTTPException:
            raise
        except (LookupError, NoResultFound):
            _error(404, "PIPELINE_PROFILE_NOT_FOUND")
        except Exception:
            _error(503, "PIPELINE_PREPARE_UNAVAILABLE")

    return router


__all__ = ["create_pipeline_workbench_router"]
