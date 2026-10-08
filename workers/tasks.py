"""One durable operation claim per job, with sanitized failure persistence."""
import os
from pathlib import Path
import time

from sqlalchemy import select, update

from packages.domain.operation_mode import operation_mode_from_env
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover, MockClient
from services.company_discovery.flywheel import discover_company_source
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError
from services.company_discovery.requests import authorize_discovery_request
from services.discovery.sources.collect import collect_source
from services.discovery.sources.greenhouse import SourceError
from services.discovery.matching import recalculate
from services.documents.generate import prepare, PreparationError
from services.audit_history import record_application_preparation_event
from workers.operations import claim, check_cancelled, OperationCancelled, OperationTimedOut, timeout
from workers.structured_logging import log_operation_finished


class FixtureOperationDisabled(Exception):
    """A queued fixture task must not run after switching to personal mode."""


def _record_supported_collection_candidates(store, operation_id, source_id, profile_ids):
    """Catalogue persisted cross-domain posting links for the authorized profiles."""
    from services.source_candidates.catalogue import record_supported_collection

    with store.engine.begin() as connection:
        rows = connection.execute(select(
            m.postings.c.id, m.postings.c.job_id, m.postings.c.source_url,
            m.postings.c.application_url, m.jobs.c.attributes,
        ).select_from(m.postings.join(
            m.jobs, m.jobs.c.id == m.postings.c.job_id,
        )).where(
            m.jobs.c.attributes["source_id"].as_string() == source_id,
            m.jobs.c.attributes["source_endpoint"].as_string() == m.postings.c.source_url,
        )).mappings()
        persisted = [dict(row) for row in rows]
        for profile_id in profile_ids:
            for posting in persisted:
                record_supported_collection(
                    connection, profile_id=profile_id,
                    request_id=identity("supported-collection", operation_id, source_id,
                                         profile_id, posting["id"]),
                    job_id=posting["job_id"], source_url=posting["source_url"],
                    application_url=posting["application_url"],
                )


def _load_saved_search_for_discovery(store, source_id, profile_id, search_profile_id):
    from services.discovery.search_profiles import get_search_profile

    if not isinstance(search_profile_id, str) or not search_profile_id:
        raise ValueError("DISCOVER_SEARCH_PROFILE_ID_INVALID")
    search_profile = get_search_profile(store, search_profile_id)
    if search_profile["profile_id"] != profile_id:
        raise ValueError("DISCOVER_SEARCH_PROFILE_PROFILE_MISMATCH")
    if not search_profile["enabled"]:
        raise ValueError("DISCOVER_SEARCH_PROFILE_DISABLED")
    if search_profile["source_ids"] and source_id not in search_profile["source_ids"]:
        raise ValueError("DISCOVER_SEARCH_PROFILE_SOURCE_MISMATCH")
    return search_profile


def _saved_search_candidate_job_ids(store, source_id, search_profile):
    """Return source-scoped jobs admitted by the persisted saved-search criteria."""
    from services.discovery.cheap_prefilter_pipeline import prefilter_search_profile_postings

    statement = select(
        m.postings.c.id,
        m.postings.c.job_id,
        m.jobs.c.title,
        m.companies.c.name.label("company"),
        m.jobs.c.description,
        m.jobs.c.location,
        m.jobs.c.skills,
    ).select_from(
        m.postings.join(m.jobs, m.jobs.c.id == m.postings.c.job_id).join(
            m.companies, m.companies.c.id == m.jobs.c.company_id,
        )
    ).where(
        m.jobs.c.attributes["source_id"].as_string() == source_id,
        m.jobs.c.attributes["source_endpoint"].as_string() == m.postings.c.source_url,
    ).order_by(m.postings.c.id)
    with store.engine.connect() as connection:
        postings = [dict(row) for row in connection.execute(statement).mappings()]
    result = prefilter_search_profile_postings(postings, search_profile)
    return tuple(sorted({posting["job_id"] for posting in result["candidates"]}))


def execute_operation(operation_id):
    operation_mode = operation_mode_from_env()
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    try:
        store.assert_workspace_mode(operation_mode)
        operation = claim(store, operation_id)
        if operation is None:
            return {"status": "ALREADY_CLAIMED_OR_CAPACITY"}
        started_at = time.monotonic()
        try:
            deadline = time.monotonic() + timeout()

            def remaining_seconds():
                check_cancelled(store, operation_id, operation["attempt_count"])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OperationTimedOut("OPERATION_TIMEOUT")
                return remaining

            def check():
                remaining_seconds()

            # Browser adapters use this same cancellation-aware monotonic
            # budget to cap each individual Playwright wait.
            check.remaining_seconds = remaining_seconds

            origin = os.environ.get("MJOBS_MOCK_ORIGIN", "")
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
                    search_profile = (
                        _load_saved_search_for_discovery(
                            store, params["source_id"], primary, params["search_profile_id"]
                        )
                        if "search_profile_id" in params else None
                    )
                    collect_source(store, params["source_id"], primary, check=check,
                                   profile_version=params.get("profile_version"))
                    _record_supported_collection_candidates(
                        store, operation_id, params["source_id"], profile_ids)
                    if search_profile is not None:
                        candidate_job_ids = _saved_search_candidate_job_ids(
                            store, params["source_id"], search_profile
                        )
                        recalculate(
                            store, primary, profile_version=params.get("profile_version"),
                            check=check, candidate_job_ids=candidate_job_ids,
                        )
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
                        tailor_fact_rewrites=operation['params'].get('tailor_fact_rewrites'),
                        include_resume_contact=operation['params'].get('include_resume_contact',False),
                        cover_letter_style=operation['params'].get('cover_letter_style','professional'),
                        cover_letter_length=operation['params'].get('cover_letter_length','normal'))
            elif operation["kind"] == "APPLY_MOCK":
                application = apply_mock(store, operation["entity_id"], origin, Path(os.environ["MJOBS_OUTPUT_DIR"]),
                           submit=operation["params"].get("submit", False), check=check,
                           debug_capture=os.environ.get("MJOBS_BROWSER_DEBUG_CAPTURE", "0") == "1")
                if application["state"] not in ("CONFIRMED", "DRY_RUN_COMPLETE"):
                    state, error = "BLOCKED", application["state"]
            else:
                raise ValueError("Unsupported operation")
            # A completed synchronous stage may omit an internal checkpoint. Do
            # not report success after the operation budget expires. Application
            # confirmation is already authoritative and must remain visible.
            if (operation["kind"] != "APPLY_MOCK"
                    or application["state"] not in ("CONFIRMED", "SUBMISSION_UNCONFIRMED")):
                check()
        except OperationTimedOut:
            state, error = "FAILED", "OPERATION_TIMEOUT"
        except OperationCancelled:
            state, error = "CANCELLED", "CANCEL_REQUESTED"
        except PublicFetchError as failure:
            state, error = "BLOCKED", failure.code
        except FixtureOperationDisabled:
            state, error = "BLOCKED", "FIXTURE_OPERATION_DISABLED"
        except Exception as failure:
            state, error = "FAILED", failure.code if isinstance(failure, (SourceError, PreparationError)) else type(failure).__name__
        with store.engine.begin() as connection:
            if state == "FAILED":
                state = "DEAD_LETTERED"
                error = error or "OPERATION_FAILED"
            finished_at = now()
            values = {"state": state, "error": error, "lease_expires_at": None,
                      "updated_at": finished_at}
            if state == "DEAD_LETTERED":
                values["dead_lettered_at"] = finished_at
            changed = connection.execute(update(m.operations).where(m.operations.c.id == operation_id,
                m.operations.c.state == "RUNNING", m.operations.c.attempt_count == operation["attempt_count"]).values(**values))
            if changed.rowcount != 1:
                return {"operation_id": operation_id, "status": "OWNERSHIP_EXPIRED"}
            store._audit(connection, operation_id,
                         "OPERATION_DEAD_LETTERED" if state == "DEAD_LETTERED" else "OPERATION_ATTEMPT_FINISHED",
                         {"state": state, "error": error,
                          "attempt_count": int(operation["attempt_count"])})
            if (state == "DEAD_LETTERED" and operation["kind"] == "PREPARE"
                    and isinstance(operation["params"].get("job_id"), str)):
                record_application_preparation_event(
                    connection, "APPLICATION_FAILED", operation["entity_id"],
                    operation["params"]["job_id"], operation_id)
        logged_operation = operation
        if operation["kind"] == "APPLY_MOCK":
            try:
                context = _resolve_application_log_context(store, operation)
            except Exception:
                # Logging context is best effort and cannot change a persisted outcome.
                context = {}
            logged_operation = {**operation, **context}
        elif operation["kind"] == "COMPANY_SOURCE_REFRESH":
            try:
                company_id = _resolve_company_source_company_id(store, operation)
            except Exception:
                # Logging context is best effort and cannot change a persisted outcome.
                company_id = None
            logged_operation = {**operation, "company_id": company_id}
        log_operation_finished(logged_operation, state, error, time.monotonic() - started_at)
        return {"operation_id": operation_id, "status": state, "error_category": error}
    finally:
        store.engine.dispose()


def _resolve_application_log_context(store, operation):
    """Read persisted provider and job/company IDs for APPLY_MOCK log context."""
    if operation.get("kind") != "APPLY_MOCK":
        return {}
    with store.engine.connect() as connection:
        row = connection.execute(select(
            m.routes.c.provider.label("ats_provider"),
            m.applications.c.job_id.label("job_id"),
            m.jobs.c.company_id.label("company_id"),
        ).select_from(m.applications.join(
            m.routes, m.applications.c.route_id == m.routes.c.id,
        ).join(
            m.jobs, m.applications.c.job_id == m.jobs.c.id,
        )).where(m.applications.c.id == operation.get("entity_id"))).mappings().one_or_none()
    return dict(row) if row is not None else {}


def _resolve_company_source_company_id(store, operation):
    """Read only the persisted company FK for COMPANY_SOURCE_REFRESH log context."""
    if operation.get("kind") != "COMPANY_SOURCE_REFRESH":
        return None
    with store.engine.connect() as connection:
        return connection.execute(select(m.company_sources.c.company_id).where(
            m.company_sources.c.id == operation.get("entity_id"))).scalar_one_or_none()
