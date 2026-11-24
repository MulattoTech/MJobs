"""Read-only full persisted catalogue enumeration under local-workspace API authority."""
from fastapi import APIRouter, HTTPException, Query


def create_job_inventory_router(store):
    router = APIRouter()

    @router.get('/profiles/{profile_id}/job-inventory')
    def inventory(profile_id: str, limit: int = Query(default=200, ge=1, le=200),
                  cursor: str | None = Query(default=None, max_length=1024),
                  snapshot_sha256: str | None = Query(default=None, pattern=r'^[a-f0-9]{64}$')):
        try:
            return store.job_inventory_page(profile_id, limit=limit, cursor=cursor, snapshot_sha256=snapshot_sha256)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    return router
