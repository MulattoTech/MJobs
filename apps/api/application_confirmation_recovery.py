"""Explicit local receipt verification without application submission."""
from fastapi import APIRouter, Body, HTTPException
from sqlalchemy.orm.exc import NoResultFound
from services.applications.confirmation_recovery import ConfirmationConflict, reconcile_confirmation


def create_application_confirmation_recovery_router(store, mock_origin, operation_mode):
    router = APIRouter()

    @router.get("/profiles/{profile_id}/applications/{application_id}/confirmation-check/latest")
    def read_confirmation(profile_id: str, application_id: str):
        # This reads only durable evidence. Reloading the dashboard must never
        # trigger a provider lookup or repeat the submission step.
        from services.applications.confirmation_recovery import latest_confirmation_check

        try:
            return {"result": latest_confirmation_check(
                store, profile_id, application_id, operation_mode=operation_mode)}
        except NoResultFound:
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"}) from None
        except ConfirmationConflict as error:
            raise HTTPException(409, {"code": str(error)}) from None

    @router.post("/profiles/{profile_id}/applications/{application_id}/confirmation-check")
    def check_confirmation(profile_id: str, application_id: str, payload: object = Body(...)):
        if not isinstance(payload, dict) or set(payload) != {"request_id"}:
            raise HTTPException(422, {"code": "CONFIRMATION_REQUEST_INVALID"})
        try:
            return reconcile_confirmation(store, profile_id, application_id, mock_origin,
                                          payload["request_id"], operation_mode=operation_mode)
        except NoResultFound:
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"}) from None
        except ConfirmationConflict as error:
            raise HTTPException(409, {"code": str(error)}) from None
        except (ValueError, TimeoutError):
            raise HTTPException(503, {"code": "CONFIRMATION_CHECK_UNAVAILABLE"}) from None

    return router
