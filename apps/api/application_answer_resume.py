"""Public exact-binding API for a reviewed mock-application resume."""
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm.exc import NoResultFound

from services.applications.answer_resume import admit_resume, read_resume, read_review
from services.applications.attempt_history import AttemptConflict


class AnswerResumeInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    request_id: str = Field(min_length=1, max_length=128,
                            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    question_id: str = Field(min_length=1, max_length=128)
    question_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    form_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    answer_id: str = Field(min_length=1, max_length=128)
    expected_profile_version: str = Field(min_length=1, max_length=128)


def create_application_answer_resume_router(store, operation_mode):
    router = APIRouter()

    def require_fixture():
        if operation_mode != "FIXTURE":
            raise HTTPException(409, {"code": "FIXTURE_OPERATION_DISABLED"})

    def conflict(error):
        raise HTTPException(409, {"code": error.code}) from None

    @router.get(
        "/profiles/{profile_id}/applications/{application_id}"
        "/answer-review-resumes/questions/{question_id}",
    )
    def answer_review(profile_id: str, application_id: str, question_id: str):
        require_fixture()
        try:
            return read_review(store, profile_id, application_id, question_id)
        except NoResultFound:
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"}) from None
        except AttemptConflict as error:
            conflict(error)

    @router.post(
        "/profiles/{profile_id}/applications/{application_id}/answer-review-resumes",
        status_code=202,
    )
    def answer_resume(profile_id: str, application_id: str, value: AnswerResumeInput):
        require_fixture()
        try:
            return admit_resume(store, profile_id, application_id, value.model_dump())
        except NoResultFound:
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"}) from None
        except AttemptConflict as error:
            conflict(error)

    @router.get(
        "/profiles/{profile_id}/applications/{application_id}"
        "/answer-review-resumes/{request_id}",
    )
    def answer_resume_read(
        profile_id: str, application_id: str, request_id: str, response: Response,
    ):
        require_fixture()
        try:
            result, receipt = read_resume(
                store, profile_id, application_id, request_id,
            )
            if result is None:
                response.status_code = 202
                return receipt
            return result
        except NoResultFound:
            raise HTTPException(404, {"code": "APPLICATION_RUN_NOT_FOUND"}) from None
        except AttemptConflict as error:
            conflict(error)

    return router
