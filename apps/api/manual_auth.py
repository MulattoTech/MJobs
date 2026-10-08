"""API routes for explicit user-started MOCK authentication windows."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from packages.persistence import models as m


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

        status = open_mock_authentication(
            store=store,
            application_id=application_id,
            attempt_id=attempt["id"],
            mock_origin=mock_origin,
            output_dir=output_dir,
        )
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
