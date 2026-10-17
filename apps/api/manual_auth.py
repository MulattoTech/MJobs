"""Explicit auth declarations and user-started MOCK authentication windows."""
from pathlib import Path
from secrets import token_hex
from time import monotonic
import sys
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from packages.persistence import models as m
from workers.operations import (OperationConcurrencyLimit, OperationCancelled,
                                OperationTimedOut, admit_inline, check_cancelled,
                                finish_inline, record_manual_auth_process, timeout)


class UserAuthCompletionRequest(BaseModel):
    """A declaration carries no session material and cannot authorize submission."""

    model_config = ConfigDict(strict=True, extra="forbid")
    user_completed_authentication: bool
    expected_route_url: str = Field(min_length=1, max_length=2048)
    expires_in_seconds: int = Field(default=900, ge=1, le=900)

    @field_validator("user_completed_authentication")
    @classmethod
    def require_explicit_completion(cls, value):
        if value is not True:
            raise ValueError("Explicit user completion is required")
        return value


def open_mock_authentication(*args, **kwargs):
    """Load the MOCK adapter lazily so unsupported runtimes keep API startup safe."""
    if sys.platform == 'win32':
        from services.applications.manual_auth_isolated import open_isolated_manual_auth_window
        kwargs.pop('on_process', None)
        return open_isolated_manual_auth_window(*args, **kwargs)
    from services.applications.mock_adapter import open_mock_authentication as open_window
    for key in ('operation_id', 'owner_token', 'budget_seconds'):
        kwargs.pop(key, None)
    return open_window(*args, **kwargs)


def create_manual_auth_router(store, mock_origin: str, output_dir: str | Path,
                              operation_mode: str) -> APIRouter:
    """Create metadata-only completion routes and the confined MOCK browser route."""
    router = APIRouter()

    def completion_action(action, profile_id, application_id, review_operation_id, **kwargs):
        from services.applications.manual_auth import ManualAuthCompletionError

        try:
            return action(store, profile_id, application_id, review_operation_id, **kwargs)
        except ManualAuthCompletionError as error:
            status = 404 if error.code == "AUTH_COMPLETION_NOT_FOUND" else 409
            raise HTTPException(status, {"code": error.code}) from None

    completion_path = (
        "/profiles/{profile_id}/applications/{application_id}"
        "/manual-auth/reviews/{review_operation_id}/completion"
    )

    @router.put(completion_path, openapi_extra={
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": UserAuthCompletionRequest.model_json_schema(),
        }}},
    })
    def declare_auth_completion(profile_id: str, application_id: str,
                                review_operation_id: str, body: Any = Body(...)):
        """Persist the user's declaration, never proof of an authenticated fetch."""
        # Validate locally to avoid echoing rejected credential/session payloads
        # through FastAPI's default validation response.
        try:
            request = UserAuthCompletionRequest.model_validate(body)
        except ValidationError:
            raise HTTPException(422, {"code": "AUTH_COMPLETION_REQUEST_INVALID"}) from None
        from services.applications.manual_auth import record_user_auth_completion

        return completion_action(
            record_user_auth_completion, profile_id, application_id, review_operation_id,
            expected_route_url=request.expected_route_url,
            expires_in_seconds=request.expires_in_seconds,
        )

    @router.get(completion_path)
    def read_auth_completion(profile_id: str, application_id: str, review_operation_id: str):
        """Restore metadata only after checking current identity and expiry."""
        from services.applications.manual_auth_recovery import restore_user_auth_completion

        return completion_action(
            restore_user_auth_completion, profile_id, application_id, review_operation_id,
        )

    @router.delete(completion_path)
    def revoke_auth_completion(profile_id: str, application_id: str, review_operation_id: str):
        from services.applications.manual_auth import revoke_user_auth_completion

        return completion_action(
            revoke_user_auth_completion, profile_id, application_id, review_operation_id,
        )

    @router.post('/profiles/{profile_id}/applications/{application_id}/manual-auth/{operation_id}/recover')
    def recover_manual_browser(profile_id: str, application_id: str, operation_id: str):
        if operation_mode != 'FIXTURE':
            raise HTTPException(409, 'Browser recovery is available only in verification mode')
        from services.applications.manual_auth_recovery import recover_browser_capacity
        from workers.operations import OperationActionError
        try:
            return recover_browser_capacity(store, profile_id, application_id, operation_id)
        except OperationActionError as error:
            status = 404 if error.code == 'BROWSER_RECOVERY_NOT_FOUND' else 409
            message = ('Saved browser ownership was not found for this application.' if status == 404
                else 'Recovery is not ready. Close the previous browser and stop its server; saved full-browser closure proof is required. Reload before retrying.')
            raise HTTPException(status, {'code': error.code, 'message': message}) from None

    @router.post("/profiles/{profile_id}/applications/{application_id}/manual-auth")
    def open_manual_auth(profile_id: str, application_id: str):
        if operation_mode != "FIXTURE":
            raise HTTPException(409, "Manual sign-in is available only in verification mode")

        application = store.application(application_id)
        if application is None or application["profile_id"] != profile_id:
            raise HTTPException(404, "Application not found")
        if application["target"] != "MOCK":
            raise HTTPException(409, "Manual sign-in is unavailable for external applications")
        if application["state"] != "AUTH_REQUIRED":
            raise HTTPException(409, "Application is not waiting for authentication")

        with store.engine.connect() as connection:
            attempt = connection.execute(
                select(m.attempts)
                .where(m.attempts.c.application_id == application_id)
                .order_by(m.attempts.c.started_at.desc(), m.attempts.c.id.desc())
            ).mappings().first()
        if attempt is None or attempt["state"] != "AUTH_REQUIRED":
            raise HTTPException(409, "No current authentication-required attempt is available")

        owner_token = token_hex(32)
        try:
            operation_id = admit_inline(
                store, "MANUAL_AUTH", application_id, owner_token=owner_token,
                manual_auth_identity={"profile_id": profile_id, "attempt_id": attempt["id"]})
        except OperationConcurrencyLimit:
            raise HTTPException(409, "Application browser is busy; close the active window and retry") from None
        except ValueError:
            raise HTTPException(503, "Manual browser ownership could not be verified on this runtime") from None
        deadline = monotonic() + timeout()

        def remaining_seconds():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise OperationTimedOut("Manual sign-in time budget expired")
            check_cancelled(store, operation_id)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise OperationTimedOut("Manual sign-in time budget expired")
            return remaining

        def check():
            remaining_seconds()

        check.remaining_seconds = remaining_seconds

        def on_process(identity):
            check()
            if not record_manual_auth_process(store, operation_id, identity, owner_token=owner_token):
                raise ValueError("BROWSER_IDENTITY_UNAVAILABLE")

        terminal, error = "FAILED", "MANUAL_AUTH_UNAVAILABLE"
        browser_closed = True
        try:
            check()
            browser_closed = False
            status = open_mock_authentication(
                store=store,
                operation_id=operation_id,
                owner_token=owner_token,
                budget_seconds=timeout(),
                application_id=application_id,
                attempt_id=attempt["id"],
                mock_origin=mock_origin,
                output_dir=output_dir,
                check=check,
                on_process=on_process,
            )
            if status == "CLOSE_UNCONFIRMED":
                terminal, error = "BLOCKED", "BROWSER_RELEASE_UNCONFIRMED"
                raise HTTPException(503, "Manual browser closure could not be confirmed; recovery is required")
            browser_closed = status in ("MANUAL_AUTH_WINDOW_CLOSED", "OPEN_UNAVAILABLE", "STALE")
            check()
            if status == "MANUAL_AUTH_WINDOW_CLOSED":
                terminal, error = "COMPLETED", None
        except OperationCancelled:
            terminal, error = "CANCELLED", "CANCEL_REQUESTED"
            raise HTTPException(409, "Manual sign-in was cancelled; reload and retry explicitly") from None
        except OperationTimedOut:
            error = "TIMEOUT"
            raise HTTPException(409, "Manual sign-in timed out; retry explicitly") from None
        finally:
            released = finish_inline(store, operation_id, terminal, error,
                                     browser_closed=browser_closed, owner_token=owner_token)
            if browser_closed and not released:
                raise HTTPException(503, "Manual browser release could not be verified; recovery is required")
        if status == "OPEN_UNAVAILABLE":
            raise HTTPException(503, "Manual sign-in window is unavailable on this runtime")
        if status == "STALE":
            raise HTTPException(409, "Authentication attempt changed; reload the application")
        if status != "MANUAL_AUTH_WINDOW_CLOSED":
            raise HTTPException(503, "Manual sign-in could not be completed on this runtime")

        return {
            "status": status,
            "application_id": application_id,
            "attempt_id": attempt["id"],
        }

    return router
