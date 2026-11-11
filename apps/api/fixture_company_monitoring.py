"""Explicit reviewed monitoring for an already-discovered fictional employer."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class FixtureMonitoringInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_version: str = Field(pattern=r'^[a-f0-9]{64}$')
    revision: int = Field(ge=0)
    poll_interval: int = Field(ge=300, le=604800)


def create_fixture_company_monitoring_router(store, mock_origin, operation_mode):
    from services.company_discovery.fixture_monitoring import configure_fixture_monitoring, read_fixture_monitoring
    router = APIRouter()
    path = '/profiles/{profile_id}/company-sources/{source_id}/fixture-monitoring'
    def action(function, profile_id, source_id, **kwargs):
        if operation_mode != 'FIXTURE':
            raise HTTPException(409, 'FIXTURE_MONITOR_WORKSPACE_REQUIRED')
        try:
            return function(store, mock_origin, profile_id, source_id, **kwargs)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
    @router.put(path)
    def configure(profile_id: str, source_id: str, value: FixtureMonitoringInput):
        return action(configure_fixture_monitoring, profile_id, source_id,
            expected_source_version=value.source_version, revision=value.revision, poll_interval=value.poll_interval)
    @router.get(path)
    def read(profile_id: str, source_id: str):
        return action(read_fixture_monitoring, profile_id, source_id)
    return router
