"""One durable operation claim per job, with sanitized failure persistence."""
import os
from pathlib import Path
import time

from sqlalchemy import and_, or_, select, update

from packages.domain.operation_errors import ai_operation_error_category
from packages.domain.operation_mode import operation_mode_from_env
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.applications.mock_adapter import apply_mock
from services.applications.attempt_history import AttemptConflict, timeout_failure_code
from services.applications.run_requests import validate_run_operation
from services.applications.answer_resume import (
    complete_resume_result,
    validate_resume_binding,
)
from services.discovery.pipeline import discover, MockClient
from services.company_discovery.flywheel import discover_company_source
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError
from services.company_discovery.requests import authorize_discovery_request
from services.discovery.sources.collect import collect_source
from services.discovery.sources.collect import SourceCollectionContractError
from services.agents.acceptance import CollectionContract
from services.agents.registry import PageBudget
from services.discovery.sources.greenhouse import SourceError
from services.discovery.sources.registry import (
    configured_source_registry,
    DEFAULT_SOURCE_ADAPTER_REGISTRY,
    SourceAdapterRegistry,
)
from services.discovery.matching import recalculate
from services.documents.generate import prepare, PreparationError
from services.documents.comparison import ComparisonUnavailable
from services.career_profile.durable_extraction import (
    DurableExtractionError, extract_for_operation, persist_result,
)
from services.audit_history import record_application_preparation_event
from services.documents.durable_comparison import (
    KIND as COMPARISON_KIND, DurableComparisonError, compare_for_operation, complete_result,
)
from services.ai.durable_evaluation import (
    KIND as AI_EVALUATION_KIND, DurableEvaluationError, evaluate_for_operation, complete_evaluation,
)
from services.ai.providers import AIProviderError, AIProviderDisabled
from services.ai.structured_output import StructuredOutputError
from services.discovery.search_history import record_search_history
from services.discovery.search_history_events import build_search_history_record
from services.discovery.search_profiles import FixtureSearchUnavailable
from workers.operations import (
    claim, check_cancelled, company_refresh_saved, dry_run_saved,
    register_operation_cancellation, unregister_operation_cancellation,
    OperationCancelled, OperationTimedOut, timeout,
)
from workers.structured_logging import log_operation_finished
from workers.retries import discovery_retry_plan
from workers.application_form_review import FormReviewError, handle_form_review
from services.applications.provider_registry import (
    ApplicationAdapterRegistry,
    configured_application_registry,
    DEFAULT_APPLICATION_ADAPTER_REGISTRY,
)
from services.applications.controlled_dry_run import ControlledDryRunError, execute as execute_controlled_dry_run


class FixtureOperationDisabled(Exception):
    """A queued fixture task must not run after switching to personal mode."""


def _classify_unexpected_failure(error):
    """Persist only fixed timeout categories; keep legacy names ambiguous."""
    return timeout_failure_code(error) or type(error).__name__


def _profile_job_ids(store, profile_id):
    with store.engine.connect() as connection:
        return set(connection.execute(select(m.matches.c.job_id).where(
            m.matches.c.profile_id == profile_id,
        )).scalars())


def _history_source(store, params, operation_mode):
    source_id = params.get("source_id")
    if isinstance(source_id, str) and source_id:
        with store.engine.connect() as connection:
            row = connection.execute(select(
                m.job_sources.c.provider, m.job_sources.c.tenant,
            ).where(m.job_sources.c.id == source_id)).mappings().one_or_none()
        if row is not None:
            return f"{row['provider']}:{row['tenant']}"
        return source_id
    return "mock-fixture" if operation_mode == "FIXTURE" else "UNKNOWN"


def _collection_controls(params, check):
    """Build host-owned browser limits from bounded, queue-safe parameters."""
    if "max_pages" not in params and "collection_contract" not in params:
        return None, None
    maximum = params.get("max_pages")
    raw = params.get("collection_contract")
    if type(maximum) is not int or not 1 <= maximum <= 100 or not isinstance(raw, dict):
        raise ValueError("DISCOVER_COLLECTION_CONTROL_INVALID")
    allowed = {"expected_count", "tail_key", "expected_tail", "required_item_fields"}
    fields = raw.get("required_item_fields", [])
    if set(raw) - allowed or not isinstance(fields, list):
        raise ValueError("DISCOVER_COLLECTION_CONTROL_INVALID")
    has_explicit_completeness_anchor = (
        raw.get("expected_count") is not None or raw.get("expected_tail") is not None
    )
    try:
        contract = CollectionContract(
            items_key="items",
            # Without an explicit expected count or tail, the collector must
            # supply an authoritative count from a complete SearchBatch.
            count_key=None if has_explicit_completeness_anchor else "count",
            expected_count=raw.get("expected_count"),
            tail_key=raw.get("tail_key"),
            expected_tail=raw.get("expected_tail"),
            required_item_fields=tuple(fields),
        )
    except (TypeError, ValueError):
        raise ValueError("DISCOVER_COLLECTION_CONTROL_INVALID") from None
    budget = PageBudget(maximum)
    check.consume_page = budget.consume
    return budget, contract


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


def _saved_search_candidate_job_ids(store, source_id, search_profile, *, captured_job_ids=None):
    """Filter only the current source or explicitly captured fixture jobs."""
    from services.discovery.cheap_prefilter_pipeline import prefilter_search_profile_postings
    from services.discovery.search_semantics import filter_jobs

    # Canonical attributes belong to the winning source, which can differ from
    # this posting's collector. Use immutable collection lineage when present.
    lineage = select(m.posting_value_candidates.c.id).join(
        m.source_quality_observations,
        m.source_quality_observations.c.id == m.posting_value_candidates.c.quality_observation_id,
    ).where(m.posting_value_candidates.c.posting_id == m.postings.c.id)
    selected_lineage = lineage.where(
        m.source_quality_observations.c.configured_source_id == source_id,
        m.source_quality_observations.c.source_endpoint == m.postings.c.source_url,
    ).exists()

    statement = select(
        m.postings.c.id,
        m.postings.c.job_id,
        m.jobs.c.title,
        m.companies.c.name.label("company"),
        m.jobs.c.description,
        m.jobs.c.location,
        m.jobs.c.skills,
        m.jobs.c.remote_type,
        m.jobs.c.attributes,
    ).select_from(
        m.postings.join(m.jobs, m.jobs.c.id == m.postings.c.job_id).join(
            m.companies, m.companies.c.id == m.jobs.c.company_id,
        )
    ).where(
        m.jobs.c.id.in_(captured_job_ids) if captured_job_ids is not None else
        or_(selected_lineage, and_(
            # Older captured rows have no candidate provenance; retain their
            # existing source/endpoint check without overriding known lineage.
            ~lineage.exists(),
            m.jobs.c.attributes["source_id"].as_string() == source_id,
            m.jobs.c.attributes["source_endpoint"].as_string() == m.postings.c.source_url,
        )),
    ).order_by(m.postings.c.id)
    with store.engine.connect() as connection:
        postings = [dict(row) for row in connection.execute(statement).mappings()]
    result = prefilter_search_profile_postings(postings, search_profile)
    candidates = filter_jobs(result["candidates"], search_profile["criteria"])
    return tuple(sorted({posting["job_id"] for posting in candidates}))


def execute_operation(operation_id, *, registry_module=None,
                      source_registry: SourceAdapterRegistry = DEFAULT_SOURCE_ADAPTER_REGISTRY,
                      application_registry: ApplicationAdapterRegistry = DEFAULT_APPLICATION_ADAPTER_REGISTRY):
    if source_registry is DEFAULT_SOURCE_ADAPTER_REGISTRY:
        source_registry = configured_source_registry(registry_module)
    if application_registry is DEFAULT_APPLICATION_ADAPTER_REGISTRY:
        application_registry = configured_application_registry(registry_module)
    operation_mode = operation_mode_from_env()
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    local_cancellation = None
    try:
        store.assert_workspace_mode(operation_mode)
        operation = claim(store, operation_id)
        if operation is None:
            return {"status": "ALREADY_CLAIMED_OR_CAPACITY"}
        local_cancellation = register_operation_cancellation(operation_id)
        started_at = time.monotonic()
        params = operation["params"] if isinstance(operation.get("params"), dict) else {}
        records_search = (operation["kind"] == "DISCOVER"
                          and params.get("rescore_only") is not True)
        # The operation entity is the profile that requested this run. Other
        # profile IDs in a source operation receive refreshed matches, but did
        # not independently request a search and must not inherit its history.
        history_profiles = [operation["entity_id"]]
        if records_search:
            search_started_at = operation.get("last_attempt_at") or now()
            history_source = _history_source(store, params, operation_mode)
            history_query = params.get("query") if isinstance(params.get("query"), str) else ""
            history_filters = params.get("filters") if isinstance(params.get("filters"), dict) else {}
            before_job_ids = {
                profile_id: _profile_job_ids(store, profile_id)
                for profile_id in history_profiles
            }
            history_results = None
            history_counts = {}
        else:
            search_started_at = None
            history_source = "UNKNOWN"
            history_query = ""
            history_filters = {}
            before_job_ids = {}
            history_results = None
        retry_failure = None
        collection_budget = None
        collection_contract = None
        collection_started_at = None
        collection_stop_reason = None
        collection_validation = None
        durable_resume_result = None
        durable_comparison_result = None
        durable_ai_result = None
        deadline = None
        try:
            deadline = time.monotonic() + timeout()

            def remaining_seconds():
                check_cancelled(
                    store, operation_id, operation["attempt_count"], local_cancellation,
                )
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
                    collection_budget, collection_contract = _collection_controls(params, check)
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
                    if search_profile is not None and search_profile["profile_id"] in history_profiles:
                        # History describes the same validated worker-start snapshot
                        # used for filtering and scoring, even if the user edits the
                        # saved search while source collection is running.
                        history_filters = dict(search_profile["criteria"])
                    collection_options = {}
                    if source_registry is not DEFAULT_SOURCE_ADAPTER_REGISTRY:
                        collection_options["registry"] = source_registry
                    if collection_contract is not None:
                        collection_options["collection_contract"] = collection_contract
                    collection_started_at = time.monotonic()
                    collection_result = collect_source(
                        store, params["source_id"], primary, check=check,
                        profile_version=params.get("profile_version"),
                        refresh_matches=search_profile is None,
                        **collection_options,
                    )
                    collection_validation = collection_result.get("collection_result")
                    if collection_validation is not None:
                        collection_stop_reason = "SUCCESS_ARTIFACT_VERIFIED"
                    history_results = collection_result.get("jobs_seen")
                    _record_supported_collection_candidates(
                        store, operation_id, params["source_id"], profile_ids)
                    if search_profile is not None:
                        candidate_job_ids = _saved_search_candidate_job_ids(
                            store, params["source_id"], search_profile
                        )
                        recalculate(
                            store, primary, profile_version=params.get("profile_version"),
                            check=check, candidate_job_ids=candidate_job_ids,
                            scoring_criteria=search_profile["criteria"],
                        )
                    for profile_id in profile_ids[1:]:
                        check()
                        recalculate(store, profile_id, check=check)
                elif params.get("rescore_only"):
                    # A queued rescore must use approvals current when the worker runs,
                    # so an intervening edit/revocation cannot publish a stale match.
                    recalculate(store, operation["entity_id"], check=check)
                else:
                    if params.get("search_profile_id"):
                        from services.discovery.search_profiles import get_fixture_search_profile
                        search_profile = get_fixture_search_profile(
                            store, params["search_profile_id"], operation["entity_id"])
                        if params.get("query") is not None or params.get("filters") is not None:
                            raise FixtureSearchUnavailable(
                                "FIXTURE_SEARCH_DESCRIPTORS_UNSUPPORTED",
                                "Use the selected saved search criteria instead of separate descriptors")
                        history_filters = dict(search_profile["criteria"])
                        captured = discover(
                            store, origin, operation["entity_id"], check=check,
                            profile_version=params.get("profile_version"), capture_only=True,
                        )
                        history_results = captured["postings_captured"]
                        candidates = _saved_search_candidate_job_ids(
                            store, None, search_profile, captured_job_ids=captured["job_ids"])
                        recalculate(
                            store, operation["entity_id"], profile_version=params.get("profile_version"),
                            check=check, candidate_job_ids=candidates,
                            scoring_criteria=search_profile["criteria"],
                        )
                    else:
                        discovery_jobs = discover(
                            store, origin, operation["entity_id"], check=check,
                            profile_version=params.get("profile_version"),
                        )
                        history_results = len(discovery_jobs)
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
                            return authorize_discovery_request(store, operation)
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
                    if params.get("discovery_request_id"):
                        discovery_kwargs["enrollment_guard"] = lambda connection: authorize_discovery_request(
                            store, operation, connection=connection)
                    result = discover_company_source(store, params["candidate"], client, **discovery_kwargs)
                finally:
                    client.close()
                if params.get("profile_id") and result.get("provider") == "mock-ats":
                    check()
                    discover(store, origin, params["profile_id"], check=check)
            elif operation["kind"] == "COMPANY_SOURCE_REFRESH":
                from services.company_discovery.monitoring import refresh_company_source
                result = refresh_company_source(store, operation, check=check)
            elif operation["kind"] == "APPLICATION_DRY_RUN":
                result = execute_controlled_dry_run(
                    store, Path(os.environ["MJOBS_OUTPUT_DIR"]), operation, check=check)
            elif operation["kind"] == "APPLICATION_FORM_REVIEW":
                route_url = operation["params"]["route_url"]

                def fetch_form_html(url):
                    check()
                    client = PublicCompanyClient(
                        url, check=check,
                        total_timeout=min(30, max(2, check.remaining_seconds())),
                    )
                    try:
                        response = client.get(url)
                        content_type = response.headers.get("content-type", "")
                        if (not client.allowed(str(response.url))
                                or content_type.split(";", 1)[0].strip().casefold() != "text/html"):
                            raise FormReviewError("FORM_REVIEW_CONTENT_TYPE_UNSUPPORTED")
                        return response.content
                    finally:
                        client.close()

                review_options = {}
                if application_registry is not DEFAULT_APPLICATION_ADAPTER_REGISTRY:
                    review_options["registry"] = application_registry
                result = handle_form_review(
                    store, operation, fetch_html=fetch_form_html, check=check,
                    **review_options,
                )
            elif operation["kind"] == "PREPARE":
                prepare(store, operation["params"]["job_id"], operation["entity_id"], Path(os.environ["MJOBS_OUTPUT_DIR"]),
                        ai_evaluation_run_id=operation['params'].get('ai_evaluation_run_id'),
                        expected_job_fingerprint=(operation["params"].get("preparation_priority") or {}).get("job_fingerprint"),
                        check=check, profile_version=operation["params"].get("profile_version"),
                        route_mode=operation["params"].get("route_mode", "DEFAULT"), route_id=operation["params"].get("route_id"),
                        tailor_resume=operation["params"].get("tailor_resume", False),
                        tailor_fact_ids=operation['params'].get('tailor_fact_ids'),
                        tailor_fact_rewrites=operation['params'].get('tailor_fact_rewrites'),
                        include_resume_contact=operation['params'].get('include_resume_contact',False),
                        cover_letter_style=operation['params'].get('cover_letter_style','professional'),
                        cover_letter_length=operation['params'].get('cover_letter_length','normal'),
                        role_lens_id=operation['params'].get('role_lens_id'),
                        expected_role_lens_version=operation['params'].get('expected_role_lens_version'),
                        cover_letter_story_ids=operation['params'].get('cover_letter_story_ids'),
                        cover_letter_story_job_fingerprint=operation['params'].get(
                            'cover_letter_story_job_fingerprint'),
                        resume_story_ids=operation['params'].get('resume_story_ids'),
                        resume_story_job_fingerprint=operation['params'].get(
                            'resume_story_job_fingerprint'))
            elif operation['kind'] == AI_EVALUATION_KIND:
                durable_ai_result = evaluate_for_operation(store, operation, check)
            elif operation["kind"] == COMPARISON_KIND:
                durable_comparison_result = compare_for_operation(
                    store, Path(os.environ["MJOBS_OUTPUT_DIR"]), operation, check)
            elif operation["kind"] == "RESUME_EXTRACT_QUEUED":
                durable_resume_result = extract_for_operation(
                    store, Path(os.environ["MJOBS_OUTPUT_DIR"]), operation, check)
            elif operation["kind"] == "APPLY_MOCK":
                validate_run_operation(store, operation)
                validate_resume_binding(store, operation)
                application = apply_mock(store, operation["entity_id"], origin, Path(os.environ["MJOBS_OUTPUT_DIR"]),
                           submit=operation["params"].get("submit", False), check=check,
                           debug_capture=os.environ.get("MJOBS_BROWSER_DEBUG_CAPTURE", "0") == "1",
                           request_id=operation_id,
                           expected_fingerprint=operation["params"].get("input_fingerprint"))
                if application["state"] not in ("CONFIRMED", "DRY_RUN_COMPLETE"):
                    state, error = "BLOCKED", application["state"]
            else:
                raise ValueError("Unsupported operation")
            history_counts = {}
            if records_search and history_results is not None:
                for profile_id in history_profiles:
                    new_count = len(_profile_job_ids(store, profile_id) - before_job_ids.get(profile_id, set()))
                    # The collector does not expose a duplicate tally. Do not
                    # infer duplicates from old matches or filtered postings.
                    history_counts[profile_id] = (new_count, None)
            # A completed synchronous stage may omit an internal checkpoint. Do
            # not report success after the operation budget expires. Application
            # confirmation is already authoritative and must remain visible.
            if (operation["kind"] != "APPLY_MOCK"
                    or application["state"] not in ("CONFIRMED", "SUBMISSION_UNCONFIRMED")):
                check()
        except AttemptConflict as failure:
            state, error = "BLOCKED", failure.code
        except ControlledDryRunError as failure:
            state, error = "BLOCKED", failure.code
        except OperationTimedOut:
            state, error = "FAILED", "OPERATION_TIMEOUT"
            if collection_budget is not None:
                collection_stop_reason = error
        except OperationCancelled:
            state, error = "CANCELLED", "CANCEL_REQUESTED"
            if collection_budget is not None:
                collection_stop_reason = error
        except DurableExtractionError as failure:
            state, error = "BLOCKED", failure.code
        except DurableComparisonError as failure:
            state, error = "BLOCKED", failure.code
        except (DurableEvaluationError, AIProviderError, AIProviderDisabled, StructuredOutputError) as failure:
            state, error = 'BLOCKED', ai_operation_error_category(getattr(failure, 'code', None))
        except ComparisonUnavailable:
            state, error = "BLOCKED", "COMPARISON_INPUT_UNAVAILABLE"
        except PublicFetchError as failure:
            state, error = "BLOCKED", failure.code
            retry_failure = failure
        except FixtureOperationDisabled:
            state, error = "BLOCKED", "FIXTURE_OPERATION_DISABLED"
        except FixtureSearchUnavailable as failure:
            state, error = "BLOCKED", failure.code
        except SourceCollectionContractError as failure:
            state, error = "BLOCKED", failure.code
            collection_stop_reason = failure.stop_reason
            result = failure.collection_result
            collection_validation = ({
                "code": result.code,
                "observed_count": result.observed_count,
                "tail_matches": result.tail_matches,
            } if result is not None else None)
        except Exception as failure:
            retry_failure = failure
            state, error = "FAILED", failure.code if isinstance(
                failure, (SourceError, PreparationError, FormReviewError)
            ) else _classify_unexpected_failure(failure)
            if collection_budget is not None:
                collection_stop_reason = error
            history_counts = {}
        with store.engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            current = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id).with_for_update()).mappings().one()
            if current["state"] != "RUNNING" or current["attempt_count"] != operation["attempt_count"]:
                return {"operation_id": operation_id, "status": "OWNERSHIP_EXPIRED"}
            if not current["lease_expires_at"] or current["lease_expires_at"] <= now():
                return {"operation_id": operation_id, "status": "OWNERSHIP_EXPIRED"}
            if current["cancel_requested"] and operation["kind"] != "APPLY_MOCK":
                state, error, retry_failure = "CANCELLED", "CANCEL_REQUESTED", None
                if collection_budget is not None:
                    collection_stop_reason = error
            # A receipt transaction already committed its complete result.
            # This follows the state/attempt fence above: a stale worker may
            # never acknowledge a newer owner, even when a receipt exists.
            result_saved = company_refresh_saved(connection, current) or dry_run_saved(connection, current)
            if result_saved:
                state, error, retry_failure = "COMPLETED", None, None
            resume_result = None
            if operation["kind"] == "APPLY_MOCK" and state == "COMPLETED":
                try:
                    resume_result = complete_resume_result(store, connection, current)
                except AttemptConflict as failure:
                    state, error = "BLOCKED", failure.code
            if operation['kind'] == AI_EVALUATION_KIND and state == 'COMPLETED':
                current_profile_version = connection.execute(select(m.profiles.c.version).where(
                    m.profiles.c.id == operation['entity_id']).with_for_update()).scalar_one()
                if durable_ai_result is None or current_profile_version != durable_ai_result[0]['profile_version']:
                    state, error = 'BLOCKED', ai_operation_error_category('AI_EVALUATION_PROFILE_CHANGED')
                else:
                    from services.ai.durable_evaluation import require_snapshot_jobs, require_snapshot_credentials
                    try:
                        require_snapshot_jobs(connection, durable_ai_result[0])
                        require_snapshot_credentials(connection, operation['entity_id'], durable_ai_result[0])
                    except DurableEvaluationError as failure:
                        state, error = 'BLOCKED', ai_operation_error_category(failure.code)
            finished_at = now()
            retry = discovery_retry_plan(connection, operation, retry_failure, finished_at)
            if retry is not None and deadline is not None and time.monotonic() >= deadline:
                # The fenced operation and any committed receipt now have
                # precedence. Recheck the monotonic deadline only for a failure
                # the discovery policy would actually retry, immediately before
                # applying that retry plan.
                state, error, retry_failure = "FAILED", "OPERATION_TIMEOUT", None
                retry = None
                history_counts = {}
                if collection_budget is not None:
                    collection_stop_reason = error
            if retry is not None:
                state, error = "QUEUED", retry["error"]
            if state == "FAILED":
                state = "DEAD_LETTERED"
                error = error or "OPERATION_FAILED"
            values = {"state": state, "error": error, "lease_expires_at": None,
                      "updated_at": finished_at,
                      "current_step": "COMPLETED" if state == "COMPLETED" else "FINALIZING",
                      "completed_at": None if retry else finished_at,
                      "next_attempt_at": None,
                      "recovery_metadata": ({"action": "NONE"} if state == "COMPLETED" else
                                            {"action": "MANUAL_REVIEW"} if state in {"DEAD_LETTERED", "BLOCKED"} else {})}
            if resume_result is not None:
                values["params"] = {**current["params"], "resume_result": resume_result}
            if retry is not None:
                values.update(retry, current_step="RETRY_WAIT", retry_count=operation["retry_count"] + 1)
            if result_saved:
                values['cancel_requested'] = False
            if state == "COMPLETED":
                values["progress"] = 100
            if state == "DEAD_LETTERED":
                values["dead_lettered_at"] = finished_at
            changed = connection.execute(update(m.operations).where(m.operations.c.id == operation_id,
                m.operations.c.state == "RUNNING", m.operations.c.attempt_count == operation["attempt_count"]).values(**values))
            if changed.rowcount != 1:
                return {"operation_id": operation_id, "status": "OWNERSHIP_EXPIRED"}
            if operation["kind"] == "RESUME_EXTRACT_QUEUED" and state == "COMPLETED":
                if durable_resume_result is None:
                    raise RuntimeError("Completed resume extraction has no result")
                persist_result(connection, operation, durable_resume_result)
            if operation["kind"] == COMPARISON_KIND and state == "COMPLETED":
                if durable_comparison_result is None or not complete_result(connection, operation, durable_comparison_result):
                    raise RuntimeError("Completed uploaded comparison has no fenced result")
            if operation['kind'] == AI_EVALUATION_KIND and state == 'COMPLETED':
                if durable_ai_result is None:
                    raise RuntimeError('Completed AI evaluation has no result')
                complete_evaluation(store, connection, operation, durable_ai_result)
            terminal_payload = {"state": state, "error": error,
                                "attempt_count": int(operation["attempt_count"])}
            if collection_budget is not None and collection_started_at is not None:
                terminal_payload["collection_budget"] = {
                    "stop_reason": collection_stop_reason or state,
                    "budget_consumed_ms": max(
                        0, int(round((time.monotonic() - collection_started_at) * 1000))),
                    "max_pages": collection_budget.maximum,
                    "pages_consumed": collection_budget.consumed,
                    "collection_result": collection_validation,
                }
            store._audit(connection, operation_id,
                         "OPERATION_RETRY_SCHEDULED" if retry else
                         "OPERATION_DEAD_LETTERED" if state == "DEAD_LETTERED" else "OPERATION_ATTEMPT_FINISHED",
                         terminal_payload)
            if records_search:
                for profile_id in history_profiles:
                    new_count, duplicate_count = history_counts.get(
                        profile_id, (None, None),
                    )
                    history_operation = {
                        **operation, "entity_id": profile_id, "state": state,
                        "error": error, "updated_at": finished_at,
                        "dead_lettered_at": finished_at if state == "DEAD_LETTERED" else None,
                    }
                    history_record = build_search_history_record(
                        history_operation,
                        {
                            "source": history_source,
                            "query": history_query,
                            "filters": history_filters,
                            "started_at": search_started_at,
                            "finished_at": finished_at,
                            "results": history_results,
                            "new_jobs": new_count,
                            "duplicates": duplicate_count,
                        },
                    )
                    if history_record is not None:
                        record_search_history(
                            store, history_record, connection=connection,
                        )
            if (state == "DEAD_LETTERED" and operation["kind"] == "PREPARE"
                    and isinstance(operation["params"].get("job_id"), str)):
                record_application_preparation_event(
                    connection, "APPLICATION_FAILED", operation["entity_id"],
                    operation["params"]["job_id"], operation_id)
        try:
            if (operation["kind"] == "DISCOVER"
                    and (state == "CANCELLED"
                         or (state == "DEAD_LETTERED" and error == "OPERATION_TIMEOUT"))):
                # The terminal interruption is already durable. Optional source
                # context enrichment must not extend its public time bound while
                # a loaded database services this second lookup.
                context = {}
            elif operation["kind"] == "COMPANY_SOURCE_REFRESH":
                context = _resolve_company_source_company_id(store, operation, full_context=True)
            else:
                context = _resolve_application_log_context(store, operation)
        except Exception:
            # Enrichment is read-only and best effort after durable persistence.
            # Never log database diagnostics or alter/replay a completed operation.
            context = {}
        logged_operation = {**operation, "log_context": context}
        log_operation_finished(logged_operation, state, error, time.monotonic() - started_at)
        return {"operation_id": operation_id, "status": state, "error_category": error}
    finally:
        if local_cancellation is not None:
            unregister_operation_cancellation(operation_id, local_cancellation)
        store.engine.dispose()


def _resolve_application_log_context(store, operation):
    """Compatibility seam for post-persistence operation context resolution."""
    from workers.operation_log_context import resolve_operation_log_context

    return resolve_operation_log_context(store, operation)


def _resolve_company_source_company_id(store, operation, *, full_context=False):
    """Compatibility lookup using the same validated read-only projection."""
    if operation.get("kind") != "COMPANY_SOURCE_REFRESH":
        return None
    context = _resolve_application_log_context(store, operation)
    return context if full_context else context.get("company_id")
