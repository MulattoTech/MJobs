"""Local-first product API; mock submission is explicit and external is off."""
from contextlib import asynccontextmanager
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import threading
from time import monotonic
from typing import Literal, Annotated
from urllib.parse import unquote
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from redis import Redis
from rq import Worker
from sqlalchemy import func, select, text
from sqlalchemy.exc import NoResultFound
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.datastructures import MutableHeaders

from packages.persistence import models as m
from packages.persistence.store import ProfileVersionConflict, Store, identity
from packages.domain.operation_mode import parse_operation_mode, operation_mode_from_env
from packages.domain.profile import ContactDetails, JobPreferences
from services.career_profile.extraction import ResumeExtractionCancelled, extract_resume
from services.discovery.pipeline import MockClient
from services.discovery.sources.collect import configure_source
from services.discovery.search_profiles import get_search_profile, list_search_profiles, save_search_profile
from services.discovery.search_semantics import filter_jobs
from services.dedupe.reviews import list_duplicate_reviews, record_review_decision
from services.dedupe.routes import load_job_routes, list_route_choices, plan_application_routes, source_provenance_inventory
from services.company_discovery.triggers import queue_company_discovery
from services.applications.knowledge import application_adapter_knowledge
from services.ai.providers import AIProviderDisabled, AIProviderError, create_provider
from services.ai.tasks import AI_TASKS, AITaskConfiguration, AITaskRouter
from services.ai.consumer import (
    AIConsumerCancelledError, AIConsumerTimeoutError, consume_ai_candidates, AIJobInput,
)
from services.ai.structured_output import StructuredOutputError
from services.ai.cost_controls import AICostPolicy, CandidateScore
from services.credentials.vault import CredentialCipher, CredentialConfigurationError
from services.company_discovery.public_client import PublicFetchError
from services.company_discovery.sources import (list_company_job_sources, set_company_source_watch,
    list_company_source_view, set_company_source_pin, set_company_source_enabled)
from services.documents.uploads import MAX_UPLOAD_BYTES, UploadIntegrityError, list_resumes, save_resume, read_resume
from workers.dispatch import enqueue_operation
from workers.operations import (
    QUEUES, OperationActionError, OperationConcurrencyLimit, admit_inline, finish_inline,
    inline_cancel_requested, request_cancel, retry_dead_letter, discard_dead_letter, limit,
)


class FactInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)
    source: str = Field(min_length=1, max_length=1000)
    verified: bool
    user_approved: bool
    confidence: float = Field(ge=0, le=100)


class CareerFactInput(FactInput):
    kind: Literal["skill", "experience", "responsibility", "education", "certification", "project", "accomplishment", "summary", "technology_experience"] = "skill"


def _uploaded_comparison_entity_id(profile_id, application_id, base_revision_id, tailored_document_id):
    scope = "\0".join((profile_id, application_id, base_revision_id, tailored_document_id))
    return "uploaded-comparison:" + hashlib.sha256(scope.encode("utf-8")).hexdigest()


class LocalOriginMiddleware:
    """Apply local-origin guards without wrapping the request receive channel."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != str(request.base_url).rstrip("/"):
            response = JSONResponse({"detail": "Cross-origin mutation blocked"}, status_code=403)
            await response(scope, receive, send)
            return
        if request.headers.get("sec-fetch-site") == "cross-site" and request.url.path.startswith(
                ("/profiles", "/documents", "/applications", "/operations", "/duplicate-reviews", "/company-sources")):
            response = JSONResponse({"detail": "Cross-site personal data access blocked"}, status_code=403)
            await response(scope, receive, send)
            return

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
                )
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


class AnswerInput(FactInput):
    question: str | None = Field(default=None, min_length=1, max_length=2000)


class ProfileInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    facts: list[CareerFactInput] = Field(default_factory=list, max_length=200)
    answers: list[AnswerInput] = Field(default_factory=list, max_length=200)
    expected_version: str | None = Field(default=None, min_length=64, max_length=64)
    contact: ContactDetails = Field(default_factory=ContactDetails)
    preferences: JobPreferences = Field(default_factory=JobPreferences)


class AITaskSettingsInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    # Validate nested keys/values through AITaskConfiguration so FastAPI's
    # default validation response cannot echo a rejected credential value.
    tasks: dict[str, dict[str, object]] = Field(max_length=16)


class AICostPolicyInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    minimum_score: int = Field(ge=0, le=100)
    max_input_candidates: int = Field(ge=1, le=100_000)
    max_ai_candidates: int = Field(ge=0, le=75)
    max_document_candidates: int = Field(ge=0, le=20)

    @model_validator(mode="after")
    def document_work_must_fit_ai_cap(self):
        if self.max_document_candidates > self.max_ai_candidates:
            raise ValueError("AI_FUNNEL_POLICY_INVALID")
        return self


class ProviderCredentialInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    secret: SecretStr = Field(min_length=1, max_length=16_384)


class AIEvaluationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)


class DiscoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=64)


class ResumeFactRewriteInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    fact_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)
    reviewed: bool


class ApplicationInput(DiscoveryInput):
    job_id: str = Field(min_length=64, max_length=64)
    route_mode: Literal["DEFAULT", "THIS_ROUTE", "EMPLOYER_SITE", "ALL_DISTINCT"] = "DEFAULT"
    route_id: str | None = Field(default=None, min_length=1, max_length=128)
    tailor_resume: bool = Field(default=False, strict=True)
    include_resume_contact: bool = Field(default=False, strict=True)
    cover_letter_length: Literal["VERY_SHORT", "CONCISE", "NORMAL", "DETAILED"] = "NORMAL"
    cover_letter_style: Literal["professional", "warm", "technical", "direct", "conversational", "formal"] = "professional"
    tailor_fact_ids: list[Annotated[str,Field(strict=True,min_length=1,max_length=128)]] | None = Field(default=None,min_length=1,max_length=1000)
    tailor_fact_rewrites: list[ResumeFactRewriteInput] | None = Field(default=None,min_length=1,max_length=100)
    expected_profile_version: str | None = Field(default=None,min_length=64,max_length=64)
    ai_evaluation_run_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode='after')
    def selected_facts_require_tailoring_and_snapshot(self):
        if self.tailor_fact_ids is not None and (not self.tailor_resume or not self.expected_profile_version):
            raise ValueError('Explicit resume facts require tailoring and the observed profile version')
        if self.tailor_fact_rewrites is not None and (
                not self.tailor_resume or self.tailor_fact_ids is None or not self.expected_profile_version):
            raise ValueError('Reviewed resume rewrites require selected facts, tailoring and the observed profile version')
        return self


class ApplicationAnswerItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    text: str = Field(min_length=1, max_length=10000)
    answer_id: str | None = Field(default=None, min_length=1, max_length=128)
    reviewed: bool

    @model_validator(mode='after')
    def explicit_review_and_text(self):
        if not self.reviewed or not self.question.strip() or not self.text.strip():
            raise ValueError('Every answer must contain user text and deliberate review')
        return self


class ApplicationAnswerExportInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128)
    items: list[ApplicationAnswerItem] = Field(min_length=1, max_length=100)


class AIDocumentReviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    job_ids: list[Annotated[str, Field(strict=True, min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_review_candidates(self):
        if len(set(self.job_ids)) != len(self.job_ids):
            raise ValueError("AI_DOCUMENT_REVIEW_INPUT_INVALID")
        return self


class ManualJobCaptureInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)
    request_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    company: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=50000)
    location: str = Field(default="Unknown", min_length=1, max_length=1000)
    remote_type: Literal["UNKNOWN", "REMOTE_WORLDWIDE", "REMOTE_US", "REMOTE_COUNTRY",
                         "REMOTE_SPECIFIC_STATES", "HYBRID", "ONSITE"] = "UNKNOWN"
    reference_url: str | None = Field(default=None, max_length=2000)
    application_url: str | None = Field(default=None, max_length=2000)
    source_details: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_manual_capture(self):
        if not self.request_id.strip() or not self.title.strip() or not self.company.strip() or not self.description.strip():
            raise ValueError("Job title, company, description, and request identifier are required")
        if self.request_id != self.request_id.strip():
            raise ValueError("Request identifier cannot have surrounding whitespace")
        if not self.reference_url and not self.application_url:
            raise ValueError("Enter a job reference URL or an application URL")
        if len(self.__pydantic_extra__ or {}) > 20:
            raise ValueError("Manual job capture accepts at most 20 additional source fields")
        size = len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
        if size > 100_000:
            raise ValueError("Manual job source data exceeds the 100 KiB limit")
        return self


class JobDispositionInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    disposition: Literal["ACTIVE", "SAVED", "HIDDEN", "NOT_INTERESTED"]
    request_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def bounded_request_id(self):
        if self.request_id != self.request_id.strip():
            raise ValueError("Tracking request identifier cannot have surrounding whitespace")
        return self


class JobNoteInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def bounded_request_id(self):
        if self.request_id != self.request_id.strip():
            raise ValueError("Tracking request identifier cannot have surrounding whitespace")
        return self


class ApplicationPipelineStatusInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    status: Literal["SAVED", "READY", "APPLIED", "RECRUITER_CONTACT", "INTERVIEW",
                    "TECHNICAL_INTERVIEW", "FINAL_INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"]
    request_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def bounded_request_id(self):
        if self.request_id != self.request_id.strip():
            raise ValueError("Tracking request identifier cannot have surrounding whitespace")
        return self


class ApplicationNoteInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def bounded_request_id(self):
        if self.request_id != self.request_id.strip():
            raise ValueError("Tracking request identifier cannot have surrounding whitespace")
        return self


class MockRunInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    submit: bool = False


class DedupeDecisionInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    action: Literal["MERGE", "KEEP_SEPARATE", "NEVER_MERGE"]


class CompanyDiscoveryInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    company_name: str = Field(min_length=1, max_length=200)
    company_website: str | None = Field(default=None, max_length=2000)
    application_url: str | None = Field(default=None, max_length=2000)
    ats_tenant: str | None = Field(default=None, max_length=128)
    reason: Literal["PREVIOUSLY_UNSEEN_COMPANY", "HIGH_MATCH_JOB", "USER_SAVED_COMPANY",
                    "USER_PREFERRED_COMPANY", "USER_ADDED_COMPANY", "NEW_ATS_TENANT",
                    "REPEATED_AGGREGATOR_APPEARANCE", "KNOWN_SOURCE_BROKEN"]
    profile_id: str | None = Field(default=None, max_length=128)

    request_id: str | None = Field(default=None, min_length=1, max_length=128)

class CompanySourcePinInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    pinned: bool


class DiscoveryRequestRevisionInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    revision: int = Field(ge=1)


class DiscoveryRequestReviewInput(DiscoveryRequestRevisionInput):
    website: str = Field(min_length=1, max_length=2000)


class CompanyMonitoringInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    source_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=0)
    poll_interval: int = Field(ge=300, le=604800)


class CompanyRefreshInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128)


class CompanySourceEnabledInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    enabled: bool


class CompanySourceWatchInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    watched: bool


class SourceInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    tenant: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    poll_interval: int = Field(default=300, ge=300, le=604800)
    concurrency: int = Field(default=1, ge=1, le=8)
    request_timeout: int = Field(default=10, ge=2, le=60)
    rate_limit: int = Field(default=60, ge=1, le=600)
    jitter_seconds: int = Field(default=30, ge=0, le=300)


class SearchProfileInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    profile_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    include_titles: list[str] = Field(default_factory=list, max_length=100)
    exclude_titles: list[str] = Field(default_factory=list, max_length=100)
    required_keywords: list[str] = Field(default_factory=list, max_length=100)
    optional_keywords: list[str] = Field(default_factory=list, max_length=100)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=100)
    exact_phrases: list[str] = Field(default_factory=list, max_length=100)
    technology_aliases: dict[str, list[str]] = Field(default_factory=dict)
    required_skills: list[str] = Field(default_factory=list, max_length=100)
    optional_skills: list[str] = Field(default_factory=list, max_length=100)
    excluded_skills: list[str] = Field(default_factory=list, max_length=100)
    company_include: list[str] = Field(default_factory=list, max_length=100)
    company_exclude: list[str] = Field(default_factory=list, max_length=100)
    remote: Literal["ANY", "REQUIRED", "EXCLUDED"] = "ANY"
    work_arrangements: list[Literal["REMOTE", "HYBRID", "ONSITE"]] = Field(default_factory=list, max_length=3)
    countries: list[str] = Field(default_factory=list, max_length=50)
    states: list[str] = Field(default_factory=list, max_length=100)
    cities: list[str] = Field(default_factory=list, max_length=100)
    remote_regions: list[str] = Field(default_factory=list, max_length=100)
    radius_miles: float | None = Field(default=None, ge=1, le=10000)
    radius_latitude: float | None = Field(default=None, ge=-90, le=90)
    radius_longitude: float | None = Field(default=None, ge=-180, le=180)
    employment_types: list[Literal["FULL_TIME", "PART_TIME", "CONTRACT", "CONTRACT_TO_HIRE",
                                   "TEMPORARY", "INTERNSHIP", "OTHER", "UNKNOWN"]] = Field(default_factory=list, max_length=20)
    experience_levels: list[Literal["INTERNSHIP", "ENTRY", "JUNIOR", "ASSOCIATE", "MID", "SENIOR",
                                    "LEAD", "MANAGER", "DIRECTOR", "EXECUTIVE", "UNKNOWN"]] = Field(default_factory=list, max_length=20)
    salary_min: float | None = Field(default=None, ge=0, le=100000000)
    salary_max: float | None = Field(default=None, ge=0, le=100000000)
    salary_period: Literal["HOUR", "YEAR"] | None = None
    salary_known_only: bool = False
    salary_unknown_allowed: bool = True
    date_window: Literal["ANY", "LAST_HOUR", "TODAY", "LAST_24_HOURS", "LAST_3_DAYS", "LAST_7_DAYS", "LAST_30_DAYS", "CUSTOM"] = "ANY"
    date_start: str | None = Field(default=None, max_length=64)
    date_end: str | None = Field(default=None, max_length=64)
    clearance_levels: list[Literal["NONE_STATED", "PUBLIC_TRUST", "SECRET", "TOP_SECRET", "TS_SCI", "OTHER"]] = Field(default_factory=list, max_length=10)
    exclude_clearance_required: bool = False
    excluded_work_authorization_flags: list[Literal["US_CITIZENSHIP_REQUIRED", "SPONSORSHIP_AVAILABLE", "NO_SPONSORSHIP", "ALREADY_AUTHORIZED_REQUIRED"]] = Field(default_factory=list, max_length=20)
    excluded_conditions: list[Literal["ON_CALL", "NIGHT_SHIFT", "CONTRACT_1099", "COMMISSION", "DOOR_TO_DOOR", "CLEARANCE_REQUIRED", "MANDATORY_RELOCATION"]] = Field(default_factory=list, max_length=20)
    max_travel_percent: float | None = Field(default=None, ge=0, le=100)
    source_ids: list[str] = Field(default_factory=list, max_length=100)


def create_app(store, mock_origin, output_dir, redis_url="redis://127.0.0.1:6379/0", operation_mode="PERSONAL"):
    operation_mode = parse_operation_mode(operation_mode)
    if operation_mode == "FIXTURE":
        guard = MockClient(mock_origin)
        guard.close()
    store.bind_workspace_mode(operation_mode)
    app = FastAPI(title="MJobs", description="Local job discovery, factual preparation and tracking. External submission disabled.")
    app.state.resume_extraction_events = {}
    app.state.resume_extraction_lock = threading.Lock()
    app.state.uploaded_comparison_events = {}
    app.state.uploaded_comparison_lock = threading.Lock()
    app.state.ai_evaluation_events = {}
    app.state.ai_evaluation_lock = threading.Lock()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "api", "testserver"])
    app.add_middleware(LocalOriginMiddleware)

    @app.exception_handler(ValueError)
    async def invalid(_, error):
        if getattr(error, 'code', None) == 'AVOID_LIST_BLOCKED':
            return JSONResponse({"detail": {"code": error.code, "message": str(error)}}, status_code=422)
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        if "/provider-credentials" in request.url.path:
            return JSONResponse({"detail": {"code": "CREDENTIAL_INPUT_INVALID"}}, status_code=422)
        if request.url.path.startswith("/profiles/") and request.url.path.endswith(
                ("/ai-task-settings", "/ai-cost-policy")):
            code = ("AI_TASK_CONFIG_INVALID" if request.url.path.endswith("/ai-task-settings")
                    else "AI_FUNNEL_POLICY_INVALID")
            return JSONResponse({"detail": {"code": code}}, status_code=422)
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(NoResultFound)
    async def missing(_, error):
        return JSONResponse({"detail": "Entity not found"}, status_code=404)

    @app.get("/health")
    def health():
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "check": "liveness", "mode": "LOCAL_FIRST", "operation_mode": operation_mode, "external_submission_enabled": False}

    @app.get("/readiness")
    def readiness():
        checks = {"database": False, "queue": False, "worker": False}
        if operation_mode == "FIXTURE":
            checks["mock"] = False
        try:
            with store.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            pass
        redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            checks["queue"] = bool(redis.ping())
            active_queues = {name for worker in Worker.all(connection=redis) if worker.get_state() in ("idle", "busy") for name in worker.queue_names()}
            checks["worker"] = set(QUEUES.values()).issubset(active_queues)
        except Exception:
            pass
        finally:
            redis.close()
        if operation_mode == "FIXTURE":
            client = MockClient(mock_origin)
            try:
                client.client.timeout = 2
                checks["mock"] = client.get(mock_origin + "/health").json().get("status") == "ok"
            except Exception:
                pass
            finally:
                client.close()
        ready = all(checks.values())
        return JSONResponse({"status": "ready" if ready else "degraded", "checks": checks}, status_code=200 if ready else 503)

    def queued(kind, entity_id, params):
        operation_id = enqueue_operation(store, kind, entity_id, params)
        with store.engine.connect() as connection:
            state = connection.execute(select(m.operations.c.state).where(m.operations.c.id == operation_id)).scalar_one()
        return {"operation_id": operation_id, "state": state}

    @app.get("/settings")
    def settings():
        return {"mode": "LOCAL_FIRST", "operation_mode": operation_mode, "external_submission_enabled": False, "browser_workers": limit("APPLY_MOCK") if operation_mode == "FIXTURE" else 0, "worker_limits": {kind: limit(kind) for kind in QUEUES}}

    @app.get("/profiles")
    def profiles():
        return {"profiles": store.profiles()}

    @app.get("/profiles/{profile_id}")
    def profile_read(profile_id: str):
        return store.profile(profile_id)

    @app.get("/profiles/{profile_id}/ai-task-settings")
    def ai_task_settings(profile_id: str):
        configured = store.ai_task_settings(profile_id)
        configuration = AITaskConfiguration.from_mapping(configured)
        return {"profile_id": profile_id, "tasks": {
            task: {"provider": configuration.for_task(task).provider,
                   "model": configuration.for_task(task).model}
            for task in AI_TASKS
        }}

    @app.put("/profiles/{profile_id}/ai-task-settings")
    def save_ai_task_settings(profile_id: str, value: AITaskSettingsInput):
        try:
            configuration = AITaskConfiguration.from_mapping({
                task: selection for task, selection in value.tasks.items()
            })
        except ValueError as error:
            raise HTTPException(422, {"code": str(error)}) from None
        for task in AI_TASKS:
            selection = configuration.for_task(task)
            if selection.provider == "disabled":
                continue
            try:
                create_provider(selection.provider)
            except AIProviderError as error:
                raise HTTPException(422, {"code": error.code}) from None
            except ValueError as error:
                raise HTTPException(422, {"code": str(error)}) from None
        selections = {
            task: {"provider": configuration.for_task(task).provider,
                   "model": configuration.for_task(task).model}
            for task in AI_TASKS
        }
        saved = store.save_ai_task_settings(profile_id, selections)
        return {"profile_id": profile_id, "tasks": saved}

    @app.get("/profiles/{profile_id}/provider-credentials")
    def list_provider_credentials(profile_id: str):
        return {"profile_id": profile_id, "credentials": store.provider_credentials(profile_id)}

    @app.put("/profiles/{profile_id}/provider-credentials/{provider}")
    def save_provider_credential(profile_id: str, provider: str, value: ProviderCredentialInput):
        if provider not in ("openai", "anthropic"):
            raise HTTPException(422, {"code": "CREDENTIAL_PROVIDER_UNSUPPORTED"})
        try:
            cipher = CredentialCipher.from_environment()
            encrypted = cipher.encrypt(profile_id, provider, value.secret.get_secret_value())
        except CredentialConfigurationError as error:
            raise HTTPException(503, {"code": error.code}) from None
        metadata = store.save_provider_credential(profile_id, provider, encrypted)
        return {"profile_id": profile_id, "provider": provider, **metadata}

    @app.delete("/profiles/{profile_id}/provider-credentials/{provider}")
    def delete_provider_credential(profile_id: str, provider: str):
        if provider not in ("openai", "anthropic"):
            raise HTTPException(422, {"code": "CREDENTIAL_PROVIDER_UNSUPPORTED"})
        if not store.delete_provider_credential(profile_id, provider):
            raise HTTPException(404, {"code": "CREDENTIAL_NOT_FOUND"})
        return {"profile_id": profile_id, "provider": provider, "deleted": True}

    @app.post("/profiles/{profile_id}/provider-credentials/{provider}/test")
    def test_provider_credential(profile_id: str, provider: str):
        if provider not in ("openai", "anthropic"):
            raise HTTPException(422, {"code": "CREDENTIAL_PROVIDER_UNSUPPORTED"})
        encrypted = store.encrypted_provider_credential(profile_id, provider)
        if encrypted is None:
            raise HTTPException(404, {"code": "CREDENTIAL_NOT_FOUND"})
        try:
            secret = CredentialCipher.from_environment().decrypt(profile_id, provider, encrypted)
        except CredentialConfigurationError as error:
            raise HTTPException(503, {"code": error.code}) from None
        from services.credentials.connection import test_provider_connection
        result = test_provider_connection(provider, secret)
        metadata = store.record_provider_credential_test(profile_id, provider, result["status"])
        if metadata is None:
            raise HTTPException(404, {"code": "CREDENTIAL_NOT_FOUND"})
        return {"profile_id": profile_id, "provider": provider,
                "status": result["status"], "code": result["code"],
                "last_tested_at": metadata["last_tested_at"]}

    @app.get("/profiles/{profile_id}/ai-cost-policy")
    def ai_cost_policy(profile_id: str):
        configured = store.ai_cost_policy(profile_id)
        policy = configured or asdict(AICostPolicy())
        return {"profile_id": profile_id, "policy": policy}

    @app.put("/profiles/{profile_id}/ai-cost-policy")
    def save_ai_cost_policy(profile_id: str, value: AICostPolicyInput):
        try:
            policy = AICostPolicy(**value.model_dump())
        except ValueError as error:
            raise HTTPException(422, {"code": str(error)}) from None
        saved = store.save_ai_cost_policy(profile_id, asdict(policy))
        return {"profile_id": profile_id, "policy": saved}

    @app.get("/profiles/{profile_id}/ai-cost-plan")
    def ai_cost_plan(profile_id: str):
        configured = store.ai_cost_policy(profile_id)
        policy = AICostPolicy(**configured) if configured else AICostPolicy()
        rows = store.jobs(profile_id=profile_id, limit=policy.max_input_candidates + 1)
        if len(rows) > policy.max_input_candidates:
            raise HTTPException(422, {"code": "AI_FUNNEL_INPUT_LIMIT"})
        scored_rows = [row for row in rows if row.get("match_score") is not None]
        candidates = [CandidateScore(
            job_id=row["id"],
            deterministic_score=int(float(row["match_score"])),
        ) for row in scored_rows]
        try:
            plan = policy.plan(candidates)
        except ValueError as error:
            raise HTTPException(422, {"code": str(error)}) from None
        return {
            "profile_id": profile_id,
            "policy": asdict(policy),
            "plan": {
                "scanned_job_count": len(rows),
                "scored_candidate_count": plan.received_count,
                "unique_count": plan.unique_count,
                "eligible_count": plan.eligible_count,
                "ai_candidate_count": len(plan.ai_candidate_ids),
                "document_candidate_count": len(plan.document_candidate_ids),
            },
            "no_model_calls": True,
        }

    @app.get("/profiles/{profile_id}/ai-cost-plan/evaluations")
    def ai_cost_evaluation_history(profile_id: str,
                                   limit: int = Query(default=20, ge=1, le=100)):
        return {"profile_id": profile_id,
                "runs": store.ai_evaluation_history(profile_id, limit)}

    @app.post("/profiles/{profile_id}/ai-cost-plan/evaluations/{run_id}/review-documents")
    def review_ai_document_candidates(profile_id: str, run_id: str,
                                      value: AIDocumentReviewInput):
        try:
            return store.review_ai_document_candidates(profile_id, run_id, value.job_ids)
        except ValueError as error:
            code = str(error)
            status = 404 if code == "AI_EVALUATION_RUN_NOT_FOUND" else (
                409 if code in ("AI_EVALUATION_PROFILE_CHANGED", "AI_DOCUMENT_CANDIDATE_LIMIT")
                else 422)
            raise HTTPException(status, {"code": code}) from None

    @app.post("/profiles/{profile_id}/ai-cost-plan/evaluate")
    async def evaluate_ai_cost_plan(profile_id: str, request: Request,
                                   value: AIEvaluationRequest | None = None):
        configured_policy = store.ai_cost_policy(profile_id)
        policy = AICostPolicy(**configured_policy) if configured_policy else AICostPolicy()
        configuration = AITaskConfiguration.from_mapping(store.ai_task_settings(profile_id))
        if configuration.for_task("job_scoring").provider == "disabled":
            raise HTTPException(409, {"code": "AI_PROVIDER_DISABLED"})
        task_selection = configuration.for_task("job_scoring")
        rows = store.jobs(profile_id=profile_id, limit=policy.max_input_candidates + 1)
        if len(rows) > policy.max_input_candidates:
            raise HTTPException(422, {"code": "AI_FUNNEL_INPUT_LIMIT"})
        profile_version = store.profile(profile_id)["version"]
        router = AITaskRouter(configuration)
        candidates = [AIJobInput(
            job_id=row["id"], deterministic_score=int(float(row["match_score"])),
            title=row["title"], company=row["company"], description=row["description"],
        ) for row in rows if row.get("match_score") is not None]
        candidate_by_id = {candidate.job_id: candidate for candidate in candidates}
        request_id = value.request_id if value else uuid4().hex
        cancel_key = (profile_id, request_id)
        cancelled = threading.Event()
        with app.state.ai_evaluation_lock:
            if cancel_key in app.state.ai_evaluation_events:
                raise HTTPException(409, {"code": "AI_EVALUATION_REQUEST_ACTIVE"})
            try:
                operation_id = admit_inline(store, "AI_EVALUATE", profile_id)
            except OperationConcurrencyLimit:
                raise HTTPException(429, {"code": "AI_CONCURRENCY_LIMIT"}) from None
            app.state.ai_evaluation_events[cancel_key] = cancelled

        def check_cancelled():
            if cancelled.is_set() or inline_cancel_requested(store, operation_id):
                cancelled.set()
                raise AIConsumerCancelledError()

        work = asyncio.create_task(asyncio.to_thread(
            consume_ai_candidates, candidates, policy, router,
            check=check_cancelled, cancellation_event=cancelled,
        ))

        def forget_finished_evaluation(_work):
            if (_work.cancelled() or cancelled.is_set()
                    or inline_cancel_requested(store, operation_id)):
                state, error = "CANCELLED", "CANCEL_REQUESTED"
            else:
                try:
                    work_error = _work.exception()
                except BaseException:
                    work_error = RuntimeError()
                if isinstance(work_error, AIConsumerTimeoutError) or (
                        isinstance(work_error, AIProviderError)
                        and work_error.code == "AI_PROVIDER_TIMEOUT"):
                    state, error = "FAILED", work_error.code
                else:
                    state, error = (("FAILED", "AI_EVALUATION_FAILED") if work_error
                                    else ("COMPLETED", None))
            finish_inline(store, operation_id, state, error)
            with app.state.ai_evaluation_lock:
                if app.state.ai_evaluation_events.get(cancel_key) is cancelled:
                    app.state.ai_evaluation_events.pop(cancel_key, None)

        work.add_done_callback(forget_finished_evaluation)
        try:
            next_cancel_poll = monotonic()
            while not work.done():
                if await request.is_disconnected():
                    cancelled.set()
                    try:
                        await work
                    except AIConsumerCancelledError:
                        pass
                    return JSONResponse({"detail": {"code": "AI_TASK_CANCELLED"}}, status_code=499)
                if monotonic() >= next_cancel_poll:
                    if inline_cancel_requested(store, operation_id):
                        cancelled.set()
                    next_cancel_poll = monotonic() + 0.25
                await asyncio.sleep(0.05)
            result = await work
            if cancelled.is_set():
                return {"cancelled": True, "request_id": request_id}
            if await request.is_disconnected():
                return JSONResponse({"detail": {"code": "AI_TASK_CANCELLED"}}, status_code=499)
        except AIConsumerCancelledError:
            if not await request.is_disconnected():
                return {"cancelled": True, "request_id": request_id}
            return JSONResponse({"detail": {"code": "AI_TASK_CANCELLED"}}, status_code=499)
        except AIConsumerTimeoutError as error:
            raise HTTPException(504, {"code": error.code}) from None
        except StructuredOutputError as error:
            raise HTTPException(502, {"code": error.code}) from None
        except ValueError as error:
            raise HTTPException(422, {"code": str(error)}) from None
        except AIProviderDisabled:
            raise HTTPException(409, {"code": "AI_PROVIDER_DISABLED"}) from None
        except AIProviderError as error:
            status = 504 if error.code == "AI_PROVIDER_TIMEOUT" else 502
            raise HTTPException(status, {"code": error.code}) from None
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await asyncio.shield(work)
            except BaseException:
                pass
            raise
        try:
            run = store.save_ai_evaluation_run(profile_id, profile_version, asdict(policy),
                                               task_selection.provider, task_selection.model, [{
                "job_id": job_id,
                "title": candidate_by_id[job_id].title,
                "company": candidate_by_id[job_id].company,
                "deterministic_score": candidate_by_id[job_id].deterministic_score,
                "category": evaluation.category,
                "rationale": evaluation.rationale,
            } for job_id, evaluation in result.evaluations])
        except ValueError as error:
            if str(error) == "AI_EVALUATION_PROFILE_CHANGED":
                raise HTTPException(409, {"code": str(error)}) from None
            raise HTTPException(422, {"code": str(error)}) from None
        return {
            "profile_id": profile_id,
            **run,
            "evaluated_candidate_count": len(result.evaluations),
            "evaluations": [{
                "job_id": job_id, "title": candidate_by_id[job_id].title,
                "company": candidate_by_id[job_id].company,
                "deterministic_score": candidate_by_id[job_id].deterministic_score,
                "category": evaluation.category,
                "rationale": evaluation.rationale,
            } for job_id, evaluation in result.evaluations],
            "document_candidate_ids": list(result.document_candidate_ids),
            "documents_created": 0,
            "deterministic_matches_unchanged": True,
        }

    @app.post("/profiles/{profile_id}/ai-cost-plan/evaluations/{request_id}/cancel")
    def cancel_ai_cost_evaluation(profile_id: str, request_id: str):
        with app.state.ai_evaluation_lock:
            cancelled = app.state.ai_evaluation_events.get((profile_id, request_id))
            if cancelled is not None:
                cancelled.set()
        return {"cancelled": cancelled is not None}
    @app.put("/profiles")
    def profile(value: ProfileInput):
        payload = value.model_dump()
        payload.pop('expected_version', None)
        for optional in ('contact', 'preferences'):
            if optional not in value.model_fields_set:
                payload.pop(optional)
        try:
            version = store.save_profile(payload, expected_version=value.expected_version,
                                         require_expected_version=True)
        except ProfileVersionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": value.id, "version": version}

    @app.get("/profiles/{profile_id}/resumes")
    def profile_resumes(profile_id: str):
        return {"resumes": list_resumes(store, profile_id)}

    async def store_profile_document(profile_id: str, request: Request, document_kind: str,
                                     series_header: str, label_header: str):
        store.profile(profile_id)
        filename = unquote(request.headers.get("x-filename", ""))
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        length = request.headers.get("content-length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_UPLOAD_BYTES):
            raise HTTPException(413, "Resume upload exceeds the 10 MiB limit")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Resume upload exceeds the 10 MiB limit")
            content.extend(chunk)
        try:
            return save_resume(store, output_dir, profile_id, filename, media_type, bytes(content),
                               series_id=request.headers.get(series_header) or None,
                               label=unquote(request.headers.get(label_header, '')) or None,
                               document_kind=document_kind)
        except UploadIntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/profiles/{profile_id}/resumes", status_code=201)
    async def profile_resume_upload(profile_id: str, request: Request):
        return await store_profile_document(profile_id, request, 'BASE_RESUME', 'x-resume-series', 'x-resume-label')

    @app.get('/profiles/{profile_id}/document-library')
    def profile_document_library(profile_id: str):
        from services.documents.library import document_library
        return document_library(store, profile_id)

    @app.post('/profiles/{profile_id}/document-library', status_code=201)
    async def profile_document_upload(profile_id: str, request: Request):
        return await store_profile_document(profile_id, request, request.headers.get('x-document-kind', ''),
                                            'x-document-series', 'x-document-label')

    @app.get('/profiles/{profile_id}/document-library/{revision_id}')
    def profile_document_download(profile_id: str, revision_id: str):
        from services.documents.library import read_library_document
        try:
            record, data = read_library_document(store, output_dir, profile_id, revision_id)
        except UploadIntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(data, media_type='application/octet-stream', headers={
            'Content-Disposition':f'attachment; filename="document-{revision_id[:12]}.{record["format"]}"'})

    @app.get("/profiles/{profile_id}/resumes/{resume_id}")
    def profile_resume_download(profile_id: str, resume_id: str):
        try:
            record, data = read_resume(store, output_dir, profile_id, resume_id)
        except UploadIntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(data, media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="base-resume-{record["id"][:12]}.{record["format"]}"'})

    @app.post('/profiles/{profile_id}/resumes/{resume_id}/extract')
    async def profile_resume_extract(profile_id: str, resume_id: str, request: Request):
        try:
            record, data = read_resume(store, output_dir, profile_id, resume_id)
        except UploadIntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc
        try:
            operation_id = admit_inline(store, "RESUME_EXTRACT", resume_id)
        except OperationConcurrencyLimit:
            raise HTTPException(429, {"code": "DOCUMENT_CONCURRENCY_LIMIT"}) from None
        cancelled = threading.Event()
        key = (profile_id, resume_id)
        with app.state.resume_extraction_lock:
            app.state.resume_extraction_events.setdefault(key, set()).add(cancelled)
        work = asyncio.create_task(asyncio.to_thread(
            extract_resume, data, record['format'],
            f"uploaded-resume:{record['id']}:sha256:{record['sha256']}", cancelled))
        try:
            client_disconnected = False
            while not work.done():
                if await request.is_disconnected():
                    client_disconnected = True
                    cancelled.set()
                if inline_cancel_requested(store, operation_id):
                    cancelled.set()
                if cancelled.is_set():
                    break
                await asyncio.sleep(0.05)
            result = await work
            if (client_disconnected or cancelled.is_set()
                    or inline_cancel_requested(store, operation_id)):
                finish_inline(store, operation_id, "CANCELLED", "CANCEL_REQUESTED")
                return JSONResponse({"detail": "Resume extraction cancelled."}, status_code=499)
            finish_inline(store, operation_id, "COMPLETED")
            return result
        except ResumeExtractionCancelled:
            finish_inline(store, operation_id, "CANCELLED", "CANCEL_REQUESTED")
            return JSONResponse({"detail": "Resume extraction cancelled."}, status_code=499)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await asyncio.shield(work)
            except BaseException:
                pass
            finish_inline(store, operation_id, "CANCELLED", "CANCEL_REQUESTED")
            raise
        except Exception:
            finish_inline(store, operation_id, "FAILED", "RESUME_EXTRACTION_FAILED")
            raise
        finally:
            with app.state.resume_extraction_lock:
                events = app.state.resume_extraction_events.get(key)
                if events is not None:
                    events.discard(cancelled)
                    if not events:
                        app.state.resume_extraction_events.pop(key, None)

    @app.post('/profiles/{profile_id}/resumes/{resume_id}/extract/cancel')
    def profile_resume_extract_cancel(profile_id: str, resume_id: str):
        if not any(resume['id'] == resume_id for resume in list_resumes(store, profile_id)):
            raise HTTPException(404, "Resume not found")
        with store.engine.connect() as connection:
            active_operations = list(connection.execute(select(
                m.operations.c.id, m.operations.c.cancel_requested,
            ).where(
                m.operations.c.kind == "RESUME_EXTRACT",
                m.operations.c.entity_id == resume_id,
                m.operations.c.state == "RUNNING",
            )).mappings())
        durable_cancelled = False
        for operation in active_operations:
            if operation["cancel_requested"]:
                durable_cancelled = True
                continue
            result = request_cancel(store, operation["id"])
            durable_cancelled = durable_cancelled or bool(result.get("cancel_requested"))
        if durable_cancelled:
            with app.state.resume_extraction_lock:
                events = tuple(app.state.resume_extraction_events.get((profile_id, resume_id), ()))
            for cancelled in events:
                cancelled.set()
        return {"cancelled": durable_cancelled}

    @app.post("/discovery", status_code=202)
    def discovery(value: DiscoveryInput):
        if operation_mode != "FIXTURE":
            raise HTTPException(409, "Fictional discovery is available only in verification mode")
        profile = store.profile(value.profile_id)
        return queued("DISCOVER", value.profile_id, {"profile_version": profile["version"], "request_id": value.request_id})

    @app.post("/matches", status_code=202)
    def matches(value: DiscoveryInput):
        profile = store.profile(value.profile_id)
        return queued("DISCOVER", value.profile_id, {"profile_version": profile["version"], "rescore_only": True, "request_id": value.request_id})

    @app.post("/companies/discovery", status_code=202)
    def company_discovery(value: CompanyDiscoveryInput):
        if value.profile_id:
            store.profile(value.profile_id)
        candidate = value.model_dump(exclude={"reason", "profile_id", "request_id"})
        try:
            return queue_company_discovery(store, candidate, value.reason, value.profile_id, public=operation_mode == "PERSONAL", request_id=value.request_id)
        except (ValueError, PublicFetchError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/company-sources/view")
    def company_source_view(profile_id: str | None = Query(default=None, min_length=1, max_length=128)):
        return {"company_sources": list_company_source_view(store, profile_id)}

    @app.get('/company-discovery/requests')
    def discovery_requests():
        from services.company_discovery.requests import list_discovery_requests
        return {'requests': list_discovery_requests(store), 'limit': 100}

    def review_request_action(function, request_id, **values):
        if operation_mode != 'PERSONAL':
            raise HTTPException(409, 'Company website review requires a personal workspace')
        try:
            return function(store, request_id, **values)
        except (ValueError, PublicFetchError) as exc:
            code = str(exc)
            raise HTTPException(404 if code == 'DISCOVERY_REQUEST_NOT_FOUND' else 409, code) from exc

    @app.post('/company-discovery/requests/{request_id}/review', status_code=202)
    def review_discovery_request(request_id: str, value: DiscoveryRequestReviewInput):
        from services.company_discovery.requests import approve_discovery_request
        return review_request_action(approve_discovery_request, request_id, **value.model_dump())

    @app.post('/company-discovery/requests/{request_id}/dismiss')
    def dismiss_discovery_request(request_id: str, value: DiscoveryRequestRevisionInput):
        from services.company_discovery.requests import dismiss_discovery_request
        return review_request_action(dismiss_discovery_request, request_id, **value.model_dump())

    def update_source_control(function, source_id, **value):
        try:
            return function(store, source_id, **value)
        except ValueError as exc:
            if str(exc) == "COMPANY_SOURCE_NOT_FOUND":
                raise HTTPException(404, "Company source not found") from exc
            raise

    @app.put("/company-sources/{source_id}/pin")
    def company_source_pin(source_id: str, value: CompanySourcePinInput):
        return update_source_control(set_company_source_pin, source_id, pinned=value.pinned)

    @app.put("/company-sources/{source_id}/enabled")
    def company_source_enabled(source_id: str, value: CompanySourceEnabledInput):
        return update_source_control(set_company_source_enabled, source_id, enabled=value.enabled)

    @app.get("/company-sources")
    def company_sources(company_id: str | None = Query(default=None, min_length=1, max_length=128)):
        return {"company_sources": list_company_job_sources(store, company_id)}

    @app.put('/company-sources/{source_id}/monitoring')
    def company_source_monitoring(source_id: str, value: CompanyMonitoringInput):
        from services.company_discovery.monitoring import configure_monitoring
        return review_request_action(configure_monitoring, source_id,
            expected_source_version=value.source_version, revision=value.revision, poll_interval=value.poll_interval)

    @app.post('/company-sources/{source_id}/refresh', status_code=202)
    def company_source_refresh(source_id: str, value: CompanyRefreshInput):
        from services.company_discovery.monitoring import queue_refresh
        return review_request_action(queue_refresh, source_id, profile_id=value.profile_id)

    @app.put("/company-sources/{source_id}/watch")
    def company_source_watch(source_id: str, value: CompanySourceWatchInput):
        try:
            return set_company_source_watch(store, source_id, watched=value.watched)
        except ValueError as exc:
            if str(exc) == "COMPANY_SOURCE_NOT_FOUND":
                raise HTTPException(404, "Company source not found") from exc
            raise HTTPException(422, str(exc)) from exc

    @app.get("/sources")
    def sources():
        from services.company_discovery.quality import source_quality
        with store.engine.connect() as connection:
            return {"sources": [{**dict(row), "quality": source_quality(connection, configured_source_id=row['id'])}
                for row in connection.execute(select(m.job_sources).order_by(m.job_sources.c.name)).mappings()]}

    @app.get("/search-profiles")
    def search_profiles(profile_id: str | None = None):
        return {"search_profiles": list_search_profiles(store, profile_id)}

    @app.get("/search-profiles/{search_profile_id}")
    def search_profile_read(search_profile_id: str):
        return get_search_profile(store, search_profile_id)

    @app.put("/search-profiles")
    def search_profile_save(value: SearchProfileInput):
        return {"id": save_search_profile(store, value.model_dump())}

    @app.put("/sources")
    def source_configure(value: SourceInput):
        return {"id": configure_source(store, **value.model_dump())}

    @app.post("/sources/{source_id}/collect", status_code=202)
    def source_collect(source_id: str, value: DiscoveryInput):
        profile = store.profile(value.profile_id)
        with store.engine.connect() as connection:
            connection.execute(select(m.job_sources.c.id).where(m.job_sources.c.id == source_id)).one()
        return queued("DISCOVER", value.profile_id, {"source_id": source_id, "profile_version": profile["version"], "request_id": value.request_id})

    @app.post("/jobs/manual")
    def manual_job_capture(value: ManualJobCaptureInput, response: Response):
        from services.discovery.manual_capture import ManualCaptureConflict, capture_manual_job
        try:
            result = capture_manual_job(store, value.model_dump(mode="json"))
        except ManualCaptureConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        response.status_code = 201 if result["created"] else 200
        return result

    @app.get("/jobs")
    def jobs(profile_id: str | None = None, q: str = Query(default="", max_length=200),
             remote_type: str | None = Query(default=None, max_length=32),
             provider: Literal["mock-ats", "greenhouse"] | None = None, sort: Literal["score", "title"] = "score",
             search_profile_id: str | None = Query(default=None, max_length=128),
             include_dismissed: bool = False,
             limit: int = Query(default=200, ge=1, le=500)):
        rows = store.jobs(profile_id, q, remote_type, provider, sort, limit)
        if search_profile_id:
            search_profile = get_search_profile(store, search_profile_id)
            if profile_id and search_profile["profile_id"] != profile_id:
                raise HTTPException(409, "Search profile belongs to a different career profile")
            rows = filter_jobs(rows, search_profile["criteria"])
        if profile_id:
            from services.tracking.jobs import list_job_tracking
            tracking = list_job_tracking(store, profile_id, [row["id"] for row in rows])
            for row in rows:
                row["tracking"] = tracking[row["id"]]
            if not include_dismissed:
                rows = [row for row in rows if row["tracking"]["disposition"] not in ("HIDDEN", "NOT_INTERESTED")]
        return {"jobs": rows}

    @app.get("/jobs/{job_id}/routes")
    def job_routes(job_id: str):
        routes = load_job_routes(store, job_id)
        with store.engine.connect() as connection:
            for route in routes:
                route["adapter_knowledge"] = application_adapter_knowledge(connection, route["id"], operation_mode=operation_mode)
        plan = plan_application_routes(routes, "ALL_DISTINCT")
        with store.engine.connect() as connection:
            postings = [dict(row) for row in connection.execute(select(m.postings).where(m.postings.c.job_id == job_id).order_by(m.postings.c.id)).mappings()]
        return {**plan, "source_records": source_provenance_inventory(postings)}

    @app.get("/jobs/{job_id}")
    def job(job_id: str, profile_id: str | None = None):
        result = store.job(job_id, profile_id)
        if profile_id:
            from services.tracking.jobs import read_job_tracking
            result["tracking"] = read_job_tracking(store, profile_id, job_id)
        return result

    @app.get("/profiles/{profile_id}/jobs/{job_id}/tracking")
    def job_tracking_read(profile_id: str, job_id: str):
        from services.tracking.jobs import read_job_tracking
        return read_job_tracking(store, profile_id, job_id)

    @app.put("/profiles/{profile_id}/jobs/{job_id}/tracking/disposition")
    def job_disposition_update(profile_id: str, job_id: str, value: JobDispositionInput):
        from services.tracking.jobs import set_job_disposition
        return set_job_disposition(store, profile_id, job_id, value.disposition, value.request_id)

    @app.post("/profiles/{profile_id}/jobs/{job_id}/tracking/notes", status_code=201)
    def job_note_create(profile_id: str, job_id: str, value: JobNoteInput):
        from services.tracking.jobs import add_job_note
        return add_job_note(store, profile_id, job_id, value.text, value.request_id)

    @app.get("/duplicate-reviews")
    def duplicate_reviews(state: str | None = Query(default=None, max_length=64)):
        return {"duplicate_reviews": list_duplicate_reviews(store, state)}

    @app.put("/duplicate-reviews/{review_id}")
    def duplicate_review_decision(review_id: str, value: DedupeDecisionInput):
        return record_review_decision(store, review_id, value.action)

    @app.get("/applications")
    def applications(profile_id: str | None = None):
        rows = store.applications(profile_id)
        if profile_id:
            from services.tracking.applications import list_application_tracking
            tracking = list_application_tracking(store, profile_id, [row["id"] for row in rows])
            for row in rows:
                value = tracking[row["id"]]
                row["pipeline_status"] = value["status"]
                row["pipeline_attribution"] = value["attribution"]
        return {"applications": rows}

    @app.get("/profiles/{profile_id}/applications/{application_id}/tracking")
    def application_tracking_read(profile_id: str, application_id: str):
        from services.tracking.applications import read_application_tracking
        return read_application_tracking(store, profile_id, application_id)

    @app.put("/profiles/{profile_id}/applications/{application_id}/tracking/status")
    def application_pipeline_status_update(profile_id: str, application_id: str,
                                           value: ApplicationPipelineStatusInput):
        from services.tracking.applications import set_application_pipeline_status
        return set_application_pipeline_status(store, profile_id, application_id,
                                               value.status, value.request_id)

    @app.post("/profiles/{profile_id}/applications/{application_id}/tracking/notes", status_code=201)
    def application_note_create(profile_id: str, application_id: str,
                                value: ApplicationNoteInput):
        from services.tracking.applications import add_application_note
        return add_application_note(store, profile_id, application_id,
                                    value.text, value.request_id)

    @app.post("/applications", status_code=202)
    def application(value: ApplicationInput):
        from services.dedupe.source_values import require_route_review
        from services.documents.tailor import eligible_rows, select_resume_rows, validate_user_rephrases
        from services.documents.errors import PreparationError
        profile = store.profile(value.profile_id)
        if value.ai_evaluation_run_id:
            try:
                store.require_reviewed_ai_document_candidate(
                    value.profile_id, value.ai_evaluation_run_id, value.job_id)
            except ValueError as error:
                code = str(error)
                status = 404 if code == "AI_EVALUATION_RUN_NOT_FOUND" else 409
                raise HTTPException(status, code) from None
        if value.expected_profile_version and value.expected_profile_version != profile['version']:
            raise HTTPException(409,'Profile changed; reload the approved facts before preparing')
        job = store.job(value.job_id)
        manual_postings = [posting for posting in job.get("postings", [])
                           if posting.get("source") == "manual"]
        if manual_postings and not any(
                (posting.get("raw") or {}).get("submitted_fields", {}).get("application_url")
                for posting in manual_postings):
            raise HTTPException(422, "Add an application URL before preparing this manually captured job")
        try:
            select_resume_rows(eligible_rows(profile),value.tailor_resume,value.tailor_fact_ids)
            if value.tailor_fact_rewrites is not None:
                validate_user_rephrases(eligible_rows(profile), value.tailor_fact_ids,
                    [rewrite.model_dump() for rewrite in value.tailor_fact_rewrites])
        except PreparationError as error:
            raise HTTPException(409,str(error)) from error
        with store.engine.connect() as connection:
            connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == value.job_id)).one()
            try:
                require_route_review(connection, value.job_id, value.route_mode, value.route_id)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
        routes = load_job_routes(store, value.job_id)
        route_plan = plan_application_routes(routes, value.route_mode, value.route_id)
        selected_routes = [option["preferred_route"] for option in route_plan["selected"]]
        if operation_mode == "PERSONAL" and any(route["provider"] == "mock-ats" for route in selected_routes):
            raise HTTPException(409, "Fictional applications are unavailable in personal mode")
        operation = queued("PREPARE", value.profile_id, {
            "job_id": value.job_id, "profile_version": profile["version"], "request_id": value.request_id,
            "route_mode": value.route_mode, "route_id": value.route_id,
            "tailor_resume": value.tailor_resume,
            "include_resume_contact": value.include_resume_contact,
            "cover_letter_length": value.cover_letter_length,
            "cover_letter_style": value.cover_letter_style,
            **({'tailor_fact_ids':value.tailor_fact_ids} if value.tailor_fact_ids is not None else {}),
            **({'tailor_fact_rewrites':[rewrite.model_dump() for rewrite in value.tailor_fact_rewrites]} if value.tailor_fact_rewrites is not None else {}),
        })
        return {**operation, "application_id": identity(value.job_id, value.profile_id),
                "route_mode": route_plan["mode"], "route_ids": [route["id"] for route in selected_routes]}

    @app.get("/profiles/{profile_id}/applications/{application_id}")
    def application_read(profile_id: str, application_id: str):
        data = store.application(application_id)
        if data["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        from services.applications.preflight import latest_preflight
        data["question_preflight"] = latest_preflight(store, application_id)
        with store.engine.connect() as connection:
            data["adapter_knowledge"] = application_adapter_knowledge(connection, data["route_id"], operation_mode=operation_mode)
            data["attempts"] = [dict(row) for row in connection.execute(select(m.attempts).where(m.attempts.c.application_id == application_id).order_by(m.attempts.c.started_at)).mappings()]
        data["route_choices"] = list_route_choices(store, application_id)
        return data

    @app.get("/profiles/{profile_id}/applications/{application_id}/documents")
    def profile_documents(profile_id: str, application_id: str):
        from services.documents.history import application_documents
        application = store.application(application_id)
        if application["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        return {"documents": application_documents(store, application_id, output_dir)}

    @app.get("/profiles/{profile_id}/applications/{application_id}/answers")
    def application_answer_bank(profile_id: str, application_id: str):
        application = store.application(application_id)
        if application["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        # Expose only the active profile bank inside its profile-scoped route.
        return {"answers": store.profile(profile_id).get("answers", [])}

    @app.post("/profiles/{profile_id}/applications/{application_id}/answers/export", status_code=201)
    def export_application_answers(profile_id: str, application_id: str, value: ApplicationAnswerExportInput):
        from services.documents.answers import export_answers
        application = store.application(application_id)
        if application["profile_id"] != profile_id or value.profile_id != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        record = export_answers(store, application_id, output_dir, value.items)
        return {"document_id": record["id"], "version": record["version"],
                "sha256": record["sha256"]["txt"], "external_submission": "OFF"}

    @app.get('/profiles/{profile_id}/applications/{application_id}/document-comparison')
    def document_comparison(profile_id: str, application_id: str, base_document_id: str, tailored_document_id: str):
        from services.documents.comparison import compare_resumes, ComparisonUnavailable
        if store.application(application_id)["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        try:
            return compare_resumes(store, output_dir, application_id, base_document_id, tailored_document_id)
        except ComparisonUnavailable as error:
            raise HTTPException(409, str(error)) from error

    @app.get('/profiles/{profile_id}/applications/{application_id}/uploaded-resume-comparison')
    async def uploaded_resume_comparison(profile_id: str, application_id: str, base_revision_id: str,
                                         tailored_document_id: str, request: Request):
        from services.documents.uploaded_comparison import compare_uploaded_resume
        from services.documents.comparison import ComparisonUnavailable
        if store.application(application_id)["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        entity_id = _uploaded_comparison_entity_id(
            profile_id, application_id, base_revision_id, tailored_document_id)
        try:
            operation_id = admit_inline(store, "UPLOADED_RESUME_COMPARE", entity_id)
        except OperationConcurrencyLimit:
            raise HTTPException(429, {"code": "DOCUMENT_CONCURRENCY_LIMIT"}) from None
        cancelled = threading.Event()
        key = (profile_id, application_id, base_revision_id, tailored_document_id)
        with app.state.uploaded_comparison_lock:
            app.state.uploaded_comparison_events.setdefault(key, set()).add(cancelled)
        work = asyncio.create_task(asyncio.to_thread(
            compare_uploaded_resume, store, output_dir, application_id, base_revision_id,
            tailored_document_id, cancelled))
        try:
            while not work.done():
                if not cancelled.is_set() and await request.is_disconnected():
                    cancellation = request_cancel(store, operation_id)
                    if cancellation.get("cancel_requested"):
                        cancelled.set()
                if inline_cancel_requested(store, operation_id):
                    cancelled.set()
                await asyncio.sleep(0.05)
            result = await work
            if cancelled.is_set():
                finish_inline(store, operation_id, "CANCELLED", "CANCEL_REQUESTED")
                return JSONResponse({"detail": "Uploaded resume comparison cancelled."}, status_code=499)
            finish_inline(store, operation_id, "COMPLETED")
            with store.engine.connect() as connection:
                state = connection.execute(select(m.operations.c.state).where(
                    m.operations.c.id == operation_id)).scalar_one_or_none()
            if state == "CANCELLED":
                return JSONResponse({"detail": "Uploaded resume comparison cancelled."}, status_code=499)
            return result
        except ResumeExtractionCancelled:
            finish_inline(store, operation_id, "CANCELLED", "CANCEL_REQUESTED")
            return JSONResponse({"detail": "Uploaded resume comparison cancelled."}, status_code=499)
        except ComparisonUnavailable as error:
            finish_inline(store, operation_id, "FAILED", "COMPARISON_UNAVAILABLE")
            raise HTTPException(409,str(error)) from error
        except asyncio.CancelledError:
            cancelled.set()
            request_cancel(store, operation_id)
            try:
                await asyncio.shield(work)
            except BaseException:
                pass
            finish_inline(store, operation_id, "CANCELLED", "CANCEL_REQUESTED")
            raise
        except Exception:
            finish_inline(store, operation_id, "FAILED", "UPLOADED_COMPARISON_FAILED")
            raise
        finally:
            with app.state.uploaded_comparison_lock:
                events = app.state.uploaded_comparison_events.get(key)
                if events is not None:
                    events.discard(cancelled)
                    if not events:
                        app.state.uploaded_comparison_events.pop(key, None)

    @app.post('/profiles/{profile_id}/applications/{application_id}/uploaded-resume-comparison/cancel')
    def cancel_uploaded_resume_comparison(profile_id: str, application_id: str,
                                          base_revision_id: str, tailored_document_id: str):
        if store.application(application_id)["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        entity_id = _uploaded_comparison_entity_id(
            profile_id, application_id, base_revision_id, tailored_document_id)
        with store.engine.connect() as connection:
            active_operations = list(connection.execute(select(
                m.operations.c.id, m.operations.c.cancel_requested,
            ).where(
                m.operations.c.kind == "UPLOADED_RESUME_COMPARE",
                m.operations.c.entity_id == entity_id,
                m.operations.c.state == "RUNNING",
            )).mappings())
        durable_cancelled = False
        for operation in active_operations:
            if operation["cancel_requested"]:
                durable_cancelled = True
            else:
                result = request_cancel(store, operation["id"])
                durable_cancelled = durable_cancelled or bool(result.get("cancel_requested"))
        key = (profile_id, application_id, base_revision_id, tailored_document_id)
        with app.state.uploaded_comparison_lock:
            events = tuple(app.state.uploaded_comparison_events.get(key, ()))
        if durable_cancelled:
            for cancelled in events:
                cancelled.set()
        return {"cancelled": durable_cancelled}

    def serve_document(document, format):
        document = dict(document)
        root = Path(output_dir).resolve()
        if document["kind"] == "cover-letter":
            from services.documents.history import validated_letter_preview
            from services.documents.errors import PreparationError
            try:
                validated_letter_preview(store, document, output_dir)
            except (OSError, ValueError, KeyError, TypeError, PreparationError) as error:
                if getattr(error, 'code', None) == 'AVOID_LIST_BLOCKED':
                    raise HTTPException(409, {"code": error.code, "message": str(error)}) from error
                raise HTTPException(409, "Cover-letter validation failed; revise and prepare a new version") from error
        app_root = (root / document["application_id"]).resolve()
        relative = document["files"].get(format)
        path = (app_root / relative).resolve() if isinstance(relative, str) else root
        if not app_root.is_relative_to(root) or not path.is_relative_to(app_root) or not path.is_file():
            raise HTTPException(409, "Document unavailable or path invalid; regenerate for review")
        if hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"].get(format):
            raise HTTPException(409, "Document integrity check failed; regenerate for review")
        from services.documents.avoid_list import validate_content as validate_avoid_list
        application = store.application(document['application_id'])
        current_profile = store.profile(application['profile_id'])
        text_relative = document['files'].get('txt')
        text_path = (app_root / text_relative).resolve() if isinstance(text_relative, str) else root
        if text_path.is_relative_to(app_root) and text_path.is_file() and hashlib.sha256(
                text_path.read_bytes()).hexdigest() == document['sha256'].get('txt'):
            try:
                validate_avoid_list(text_path.read_text(encoding='utf-8'),
                    current_profile.get('preferences'),
                    'application answer' if document['kind'] == 'application-answer-export'
                    else 'resume' if document['kind'] == 'resume' else 'cover letter')
            except ValueError as error:
                if getattr(error, 'code', None) == 'AVOID_LIST_BLOCKED':
                    raise HTTPException(409, {"code": error.code, "message": str(error)}) from error
                raise
        media = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "txt": "text/plain"}[format]
        return FileResponse(path, media_type=media, filename=f"{document['kind']}-v{document['version']}.{format}")

    @app.get("/profiles/{profile_id}/documents/{document_id}/{format}")
    def profile_download(profile_id: str, document_id: str, format: Literal["pdf", "docx", "txt"]):
        with store.engine.connect() as connection:
            document = connection.execute(select(m.documents).where(
                m.documents.c.id == document_id)).mappings().one_or_none()
        if document is None:
            raise NoResultFound("Document is unavailable")
        application = store.application(document["application_id"])
        if application["profile_id"] != profile_id:
            raise NoResultFound("Document is not owned by this profile")
        return serve_document(document, format)

    @app.post("/profiles/{profile_id}/applications/{application_id}/mock-run", status_code=202)
    def mock_run(profile_id: str, application_id: str, value: MockRunInput):
        if operation_mode != "FIXTURE":
            raise HTTPException(409, "Fictional application actions are available only in verification mode")
        application = store.application(application_id)
        if application["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        if application["target"] != "MOCK":
            raise HTTPException(409, "External submission is disabled; this application supports manual preparation only")
        return queued("APPLY_MOCK", application_id, {"submit": value.submit})

    @app.get("/operations")
    def operations(profile_id: str | None = None):
        with store.engine.connect() as connection:
            statement = select(m.operations)
            if profile_id:
                app_ids = select(m.applications.c.id).where(m.applications.c.profile_id == profile_id)
                statement = statement.where((m.operations.c.entity_id == profile_id) | m.operations.c.entity_id.in_(app_ids))
            return {"operations": [dict(row) for row in connection.execute(statement.order_by(m.operations.c.updated_at.desc()).limit(100)).mappings()]}

    @app.get("/operations/dead-letter-queue")
    def dead_letter_queue(profile_id: str | None = None):
        statement = select(m.operations).where(m.operations.c.state == "DEAD_LETTERED")
        if profile_id:
            app_ids = select(m.applications.c.id).where(m.applications.c.profile_id == profile_id)
            statement = statement.where((m.operations.c.entity_id == profile_id) | m.operations.c.entity_id.in_(app_ids))
        with store.engine.connect() as connection:
            rows = list(connection.execute(statement.add_columns(func.count().over().label("queue_count")).order_by(
                m.operations.c.dead_lettered_at.desc().nullslast(), m.operations.c.updated_at.desc()).limit(100)).mappings())
            count = int(rows[0]["queue_count"]) if rows else 0
            operations = []
            for row in rows:
                item = dict(row)
                item.pop("queue_count", None)
                if item["kind"] == "APPLY_MOCK":
                    application_state = connection.execute(select(m.applications.c.state).where(
                        m.applications.c.id == item["entity_id"])).scalar_one_or_none()
                    item["retryable"] = application_state in ("PREPARED", "DRY_RUN_COMPLETE")
                    if application_state in ("SUBMITTING", "SUBMISSION_UNCONFIRMED"):
                        item["retry_blocked_reason"] = "RECONCILIATION_REQUIRED"
                    elif application_state not in ("PREPARED", "DRY_RUN_COMPLETE"):
                        item["retry_blocked_reason"] = "APPLICATION_REVIEW_REQUIRED"
                else:
                    item["retryable"] = item["kind"] in QUEUES
                    if not item["retryable"]:
                        item["retry_blocked_reason"] = "OPERATION_NOT_RETRYABLE"
                operations.append(item)
            return {"count": count, "operations": operations}

    @app.get("/operations/{operation_id}")
    def operation(operation_id: str):
        with store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one())

    @app.post("/operations/{operation_id}/retry", status_code=202)
    def retry_operation(operation_id: str):
        try:
            return retry_dead_letter(store, operation_id)
        except OperationActionError as error:
            status = 404 if error.code == "OPERATION_NOT_FOUND" else 409
            return JSONResponse({"detail": {"code": error.code}}, status_code=status)

    @app.post("/operations/{operation_id}/discard")
    def discard_operation(operation_id: str):
        try:
            return discard_dead_letter(store, operation_id)
        except OperationActionError as error:
            status = 404 if error.code == "OPERATION_NOT_FOUND" else 409
            return JSONResponse({"detail": {"code": error.code}}, status_code=status)

    @app.post("/operations/{operation_id}/cancel", status_code=202)
    def cancel(operation_id: str):
        result = request_cancel(store, operation_id)
        if result.get("reason") == "RECONCILIATION_REQUIRED":
            return JSONResponse(result, status_code=409)
        return result

    web = Path(__file__).resolve().parents[1] / "web" / "dist"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


def create_from_env():
    operation_mode = operation_mode_from_env()
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    try:
        app = create_app(store, os.environ["MJOBS_MOCK_ORIGIN"], Path(os.environ["MJOBS_OUTPUT_DIR"]), os.environ["MJOBS_REDIS_URL"], operation_mode=operation_mode)
    except Exception:
        store.engine.dispose()
        raise

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            store.engine.dispose()
    app.router.lifespan_context = lifespan
    return app
