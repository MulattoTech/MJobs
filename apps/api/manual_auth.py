"""API routes for explicit user-started MOCK authentication windows."""
from pathlib import Path
from time import monotonic

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from packages.persistence import models as m
from workers.operations import (OperationConcurrencyLimit, OperationCancelled,
                                OperationTimedOut, admit_inline, check_cancelled,
                                finish_inline, timeout)


def open_mock_authentication(*args, **kwargs):
    """Load the MOCK adapter lazily so unsupported runtimes keep API startup safe."""
    from services.applications.mock_adapter import open_mock_authentication as open_window

    return open_window(*args, **kwargs)


def create_manual_auth_router(store, mock_origin: str, output_dir: str | Path,
                              operation_mode: str) -> APIRouter:
    """Create the narrowly scoped MOCK manual-auth route for ``create_app``."""
    router = APIRouter()

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

        try:
            operation_id = admit_inline(store, "MANUAL_AUTH", application_id)
        except OperationConcurrencyLimit:
            raise HTTPException(409, "Application browser is busy; close the active window and retry") from None
        deadline = monotonic() + timeout()

        def check():
            if monotonic() >= deadline:
                raise OperationTimedOut("Manual sign-in time budget expired")
            check_cancelled(store, operation_id)

        terminal, error = "FAILED", "MANUAL_AUTH_UNAVAILABLE"
        browser_closed = True
        try:
            check()
            browser_closed = False
            status = open_mock_authentication(
                store=store,
                application_id=application_id,
                attempt_id=attempt["id"],
                mock_origin=mock_origin,
                output_dir=output_dir,
                check=check,
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
            finish_inline(store, operation_id, terminal, error, browser_closed=browser_closed)
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
