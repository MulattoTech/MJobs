"""One durable operation claim per job, with sanitized failure persistence."""
import os
from pathlib import Path

from sqlalchemy import select, update

from packages.domain.operation_mode import operation_mode_from_env
from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover, MockClient
from services.company_discovery.flywheel import discover_company_source
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError
from services.company_discovery.requests import authorize_discovery_request
from services.discovery.sources.collect import collect_source
from services.discovery.sources.greenhouse import SourceError
from services.discovery.matching import recalculate
from services.documents.generate import prepare, PreparationError
from workers.operations import claim, check_cancelled, OperationCancelled, timeout


class FixtureOperationDisabled(Exception):
    """A queued fixture task must not run after switching to personal mode."""


def execute_operation(operation_id):
    operation_mode = operation_mode_from_env()
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    try:
        store.assert_workspace_mode(operation_mode)
        operation = claim(store, operation_id)
        if operation is None:
            return {"status": "ALREADY_CLAIMED_OR_CAPACITY"}
        try:
            origin = os.environ.get("MJOBS_MOCK_ORIGIN", "")
            check = lambda: check_cancelled(store, operation_id)
            check()
            state, error = "COMPLETED", None
            if operation_mode == "PERSONAL":
                if operation["kind"] == "APPLY_MOCK" or (operation["kind"] == "COMPANY_DISCOVER"
                        and operation["params"].get("fetch_mode") != "PUBLIC_HTTPS") or (operation["kind"] == "DISCOVER"
                        and not operation["params"].get("source_id")
                        and not operation["params"].get("rescore_only")):
                    raise FixtureOperationDisabled()
                if operation["kind"] == "PREPARE":
                    with store.engine.connect() as connection:
                        if connection.execute(select(m.routes.c.id).where(
                                m.routes.c.job_id == operation["params"]["job_id"],
                                m.routes.c.provider == "mock-ats")).first():
                            raise FixtureOperationDisabled()
            elif operation["kind"] == "COMPANY_SOURCE_REFRESH":
                raise FixtureOperationDisabled()
            if operation["kind"] == "DISCOVER":
                params = operation["params"]
                if params.get("source_id"):
                    profile_ids = params.get("profile_ids") or [operation["entity_id"]]
                    if not isinstance(profile_ids, list) or not profile_ids or any(
                            not isinstance(profile_id, str) or not profile_id for profile_id in profile_ids):
                        raise ValueError("Invalid scheduled profile set")
                    primary = profile_ids[0]
                    collect_source(store, params["source_id"], primary, check=check,
                                   profile_version=params.get("profile_version"))
                    for profile_id in profile_ids[1:]:
                        check()
                        recalculate(store, profile_id, check=check)
                elif params.get("rescore_only"):
                    # A queued rescore must use approvals current when the worker runs,
                    # so an intervening edit/revocation cannot publish a stale match.
                    recalculate(store, operation["entity_id"], check=check)
                else:
                    discover(store, origin, operation["entity_id"], check=check, profile_version=params.get("profile_version"))
            elif operation["kind"] == "COMPANY_DISCOVER":
                params = operation["params"]
                if params.get("monitoring_source_id") or params.get("discovery_request_id"):
                    cancellation_check = check
                    def check():
                        cancellation_check()
                        if params.get("monitoring_source_id"):
                            from services.company_discovery.monitoring import authorize_rediscovery
                            authorize_rediscovery(store, operation)
                        if params.get("discovery_request_id"):
                            authorize_discovery_request(store, operation)
                    check()
                if operation_mode == "PERSONAL":
                    client = PublicCompanyClient(params["candidate"].get("company_website"), check=check, total_timeout=min(90, timeout()-5))
                else:
                    if params.get("fetch_mode") == "PUBLIC_HTTPS":
                        raise FixtureOperationDisabled()
                    client = MockClient(origin, check=check)
                try:
                    discovery_kwargs = dict(check=check, profile_id=params.get("profile_id"),
                                            profile_version=params.get("profile_version"))
                    if params.get("monitoring_source_id"):
                        discovery_kwargs["preserve_source_id"] = params.get("monitoring_source_id")
                        discovery_kwargs["enrollment_guard"] = lambda connection: authorize_rediscovery(store, operation, connection=connection)
                    result = discover_company_source(store, params["candidate"], client, **discovery_kwargs)
                finally:
                    client.close()
                if params.get("profile_id") and result.get("provider") == "mock-ats":
                    check()
                    discover(store, origin, params["profile_id"], check=check)
            elif operation["kind"] == "COMPANY_SOURCE_REFRESH":
                from services.company_discovery.monitoring import refresh_company_source
                result = refresh_company_source(store, operation, check=check)
            elif operation["kind"] == "PREPARE":
                prepare(store, operation["params"]["job_id"], operation["entity_id"], Path(os.environ["MJOBS_OUTPUT_DIR"]),
                        check=check, profile_version=operation["params"].get("profile_version"),
                        route_mode=operation["params"].get("route_mode", "DEFAULT"), route_id=operation["params"].get("route_id"),
                        tailor_resume=operation["params"].get("tailor_resume", False),
                        tailor_fact_ids=operation['params'].get('tailor_fact_ids'),
                        include_resume_contact=operation['params'].get('include_resume_contact',False),
                        cover_letter_style=operation['params'].get('cover_letter_style','professional'),
                        cover_letter_length=operation['params'].get('cover_letter_length','normal'))
            elif operation["kind"] == "APPLY_MOCK":
                application = apply_mock(store, operation["entity_id"], origin, Path(os.environ["MJOBS_OUTPUT_DIR"]),
                           submit=operation["params"].get("submit", False), check=check)
                if application["state"] not in ("CONFIRMED", "DRY_RUN_COMPLETE"):
                    state, error = "BLOCKED", application["state"]
            else:
                raise ValueError("Unsupported operation")
        except OperationCancelled:
            state, error = "CANCELLED", "CANCEL_REQUESTED"
        except PublicFetchError as failure:
            state, error = "BLOCKED", failure.code
        except FixtureOperationDisabled:
            state, error = "BLOCKED", "FIXTURE_OPERATION_DISABLED"
        except Exception as failure:
            state, error = "FAILED", failure.code if isinstance(failure, (SourceError, PreparationError)) else type(failure).__name__
        with store.engine.begin() as connection:
            changed = connection.execute(update(m.operations).where(m.operations.c.id == operation_id,
                m.operations.c.state == "RUNNING", m.operations.c.attempt_count == operation["attempt_count"]).values(state=state, error=error, lease_expires_at=None, updated_at=now()))
            if changed.rowcount != 1:
                return {"operation_id": operation_id, "status": "OWNERSHIP_EXPIRED"}
        return {"operation_id": operation_id, "status": state, "error_category": error}
    finally:
        store.engine.dispose()
