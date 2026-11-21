"""Profile-owned fact rejection routes with version/content conflict checks."""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from services.career_profile.fact_rejections import FactRejectionError, list_fact_rejections, reject_fact


class FactRejectionInput(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    decision: Literal['REJECT']
    confirmed: bool
    expected_profile_version: str = Field(pattern=r'^[0-9a-f]{64}$')
    fact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def create_fact_rejections_router(store):
    router = APIRouter()

    @router.get('/profiles/{profile_id}/fact-rejections')
    def read(profile_id: str):
        try:
            return list_fact_rejections(store, profile_id)
        except FactRejectionError as error:
            raise HTTPException(error.status_code, detail={'code': error.code}) from None

    @router.post('/profiles/{profile_id}/facts/{fact_id}/reject')
    def reject(profile_id: str, fact_id: str, value: FactRejectionInput):
        try:
            return reject_fact(store, profile_id, fact_id,
                expected_profile_version=value.expected_profile_version,
                expected_fact_sha256=value.fact_sha256, confirmed=value.confirmed)
        except FactRejectionError as error:
            raise HTTPException(error.status_code, detail={'code': error.code}) from None

    return router
