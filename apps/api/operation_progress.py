"""Profile-scoped read API for safe persistent operation progress."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from packages.persistence import models as m
from services.operations.progress import get_operation_progress


_PUBLIC_FIELDS = (
    "operation_id", "operation_type", "entity_id", "state", "current_step",
    "progress", "started_at", "updated_at", "completed_at", "retry_count",
    "recovery_summary", "next_attempt_at",
)


def create_operation_progress_router(store) -> APIRouter:
    """Create a bounded, read-only operations view owned by one profile."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/operations/progress")
    def operation_progress(profile_id: str, limit: int = Query(default=100, ge=1, le=200)):
        with store.engine.connect() as connection:
            profile_exists = connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == profile_id)).scalar_one_or_none()
            if profile_exists is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "OPERATION_PROFILE_NOT_FOUND"},
                )

            owned_applications = select(m.applications.c.id).where(
                m.applications.c.profile_id == profile_id)
            owned_resumes = select(m.uploaded_resumes.c.id).where(
                m.uploaded_resumes.c.profile_id == profile_id)
            operation_ids = list(connection.execute(
                select(m.operations.c.id).where(
                    (m.operations.c.entity_id == profile_id)
                    | m.operations.c.entity_id.in_(owned_applications)
                    | m.operations.c.entity_id.in_(owned_resumes)
                ).order_by(m.operations.c.updated_at.desc(), m.operations.c.id.desc())
                .limit(limit)
            ).scalars())

        operations = []
        for operation_id in operation_ids:
            # The service read model excludes raw params, error details and metadata.
            # Keep this API allowlist fixed if the internal model gains new fields.
            public = get_operation_progress(store, operation_id)
            operations.append({field: public.get(field) for field in _PUBLIC_FIELDS})
        return {"operations": operations}

    return router
