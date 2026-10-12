"""Local-first product API; mock submission is explicit and external is off."""
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from time import monotonic
from typing import Literal, Annotated
from urllib.parse import unquote
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Query
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
from services.career_profile.durable_extraction import (
    DurableExtractionError, OPERATION_KIND as DURABLE_RESUME_EXTRACT_KIND,
    read_extraction as read_durable_extraction, queue_extraction as queue_durable_extraction,
)
from services.discovery.pipeline import MockClient
from services.discovery.sources.collect import configure_source
from services.discovery.search_profiles import (
    CLEARANCE_LEVELS, EDUCATION_REQUIREMENTS, get_search_profile, list_search_profiles, save_search_profile,
)
from services.discovery.search_semantics import filter_jobs
from services.discovery.skill_taxonomy import SKILL_TAXONOMY, canonical_skill_id
from services.tracking.applications import PIPELINE_STATUSES
from services.documents.answers import (
    DEFAULT_ANSWER_CONFIDENCE_POLICY, get_answer_confidence_policy,
    save_answer_confidence_policy,
)
from services.dedupe.reviews import list_duplicate_reviews, record_review_decision
from services.dedupe.routes import (load_job_routes, list_route_choices, plan_application_routes,
                                    route_authority, save_verified_redirect_destination,
                                    source_provenance_inventory)
from services.dedupe.route_redirects import resolve_aggregator_redirect
from services.company_discovery.triggers import queue_company_discovery
from services.applications.knowledge import application_adapter_knowledge
from services.ai.providers import AIProviderError, create_provider
from services.ai.tasks import AI_TASKS, AITaskConfiguration, AITaskRouter, fallback_capabilities
from services.ai.cost_controls import AICostPolicy, CandidateScore
from services.credentials.vault import CredentialCipher, CredentialConfigurationError
from services.company_discovery.public_client import PublicFetchError
from services.company_discovery.sources import (list_company_job_sources, set_company_source_watch,
    list_company_source_view, set_company_source_pin, set_company_source_enabled)
from services.documents.uploads import MAX_UPLOAD_BYTES, UploadIntegrityError, list_resumes, save_resume, read_resume
from services.documents.alignment_preview import build_alignment_preview
from services.documents.tailor import target_fingerprint
from services.audit_history import list_audit_history
from workers.dispatch import enqueue_operation
from workers.operations import (
    QUEUES, OperationActionError, request_cancel, retry_dead_letter, discard_dead_letter, limit,
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


class AnswerConfidencePolicyInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    minimum_saved_answer_confidence: int = Field(ge=80, le=100)
    minimum_question_match_confidence: float = Field(ge=0.75, le=1)


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
    search_profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    query: str | None = Field(default=None, max_length=2000)
    filters: dict[str, object] | None = Field(default=None, max_length=50)


class ResumeFactRewriteInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    fact_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)
    reviewed: bool


class ResumeExtractionQueueInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    request_id: str = Field(min_length=16, max_length=128)


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
    expected_job_fingerprint: str | None = Field(default=None,min_length=64,max_length=64,pattern=r'^[0-9a-f]{64}$')
    ai_evaluation_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    role_lens_id: str | None = Field(default=None, strict=True, min_length=1, max_length=32,
                                     pattern=r'^[a-z][a-z0-9_]{0,31}$')
    expected_role_lens_version: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    cover_letter_story_ids: list[Annotated[str,Field(strict=True,min_length=1,max_length=128)]] | None = Field(default=None,min_length=1,max_length=50)
    resume_story_ids: list[Annotated[str,Field(strict=True,min_length=1,max_length=128)]] | None = Field(default=None,min_length=1,max_length=50)

    @model_validator(mode='after')
    def selected_facts_require_tailoring_and_snapshot(self):
        if self.tailor_fact_ids is not None and (not self.tailor_resume or not self.expected_profile_version):
            raise ValueError('Explicit resume facts require tailoring and the observed profile version')
        if self.tailor_fact_rewrites is not None and (
                not self.tailor_resume or self.tailor_fact_ids is None or not self.expected_profile_version):
            raise ValueError('Reviewed resume rewrites require selected facts, tailoring and the observed profile version')
        if self.expected_job_fingerprint is not None and (
                not (self.tailor_resume or self.cover_letter_story_ids or self.resume_story_ids)
                or not self.expected_profile_version):
            raise ValueError('A document preparation snapshot requires tailoring or selected stories and the observed profile version')
        if (self.role_lens_id is None) != (self.expected_role_lens_version is None):
            raise ValueError('A role lens requires its observed version')
        if self.cover_letter_story_ids is not None and (
                len(set(self.cover_letter_story_ids)) != len(self.cover_letter_story_ids)
                or not self.expected_profile_version or not self.expected_job_fingerprint):
            raise ValueError('Selected cover-letter stories require unique IDs and observed profile and job versions')
        if self.resume_story_ids is not None and (
                len(set(self.resume_story_ids)) != len(self.resume_story_ids)
                or not self.expected_profile_version or not self.expected_job_fingerprint):
            raise ValueError('Selected resume stories require unique IDs and observed profile and job versions')
        return self


class PreferredApplicationRouteInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    route_id: str = Field(min_length=1, max_length=128)
    preferred: bool
    request_id: str = Field(min_length=1, max_length=128)


class ExternalApplicationInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    profile_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=64)
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    source: Literal["MANUAL", "REFERRAL", "BROWSER", "OTHER"]
    applied_at: str = Field(min_length=20, max_length=40)
    application_url: str | None = Field(default=None, max_length=2000)
    source_url: str | None = Field(default=None, max_length=2000)
    discovery_origin: str | None = Field(default=None, max_length=253)
    observed_hops: list[str] = Field(default_factory=list, max_length=20)
    job_description: str | None = Field(default=None, max_length=50000)

    @model_validator(mode="after")
    def complete_user_report(self):
        if (not self.profile_id.strip() or not self.request_id.strip()
                or self.request_id != self.request_id.strip()
                or not self.company.strip() or not self.title.strip()):
            raise ValueError("Profile, request identifier, company, and title are required")
        try:
            parsed = datetime.fromisoformat(self.applied_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Application timestamp must be ISO 8601") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Application timestamp must include a timezone")
        if self.job_description is not None and not self.job_description.strip():
            raise ValueError("Job description must contain text when provided")
        return self


class SourceCandidateReviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    state: Literal["NEEDS_REVIEW", "INTEGRATION_QUEUED", "BLOCKED", "REJECTED"]
    site_type: Literal["JOB_BOARD", "EMPLOYER", "ATS", "UNKNOWN"]
    reason: str = Field(min_length=1, max_length=1000)
    api_feed_evidence_status: Literal["PUBLIC_DOCUMENTED", "UNKNOWN", "BLOCKED"]
    api_feed_evidence_reference: str | None = Field(default=None, max_length=2000)
    api_feed_evidence_note: str = Field(min_length=1, max_length=1000)
    access_terms_note: str = Field(min_length=1, max_length=1000)
    capabilities_limits_note: str = Field(min_length=1, max_length=1000)
    expected_value_maintenance_note: str = Field(min_length=1, max_length=1000)
    integration_issue: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    request_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def bounded_review(self):
        notes = (self.reason, self.api_feed_evidence_note, self.access_terms_note,
                 self.capabilities_limits_note, self.expected_value_maintenance_note)
        if (any(not note.strip() for note in notes)
                or self.request_id != self.request_id.strip()):
            raise ValueError("Every source evaluation note and unpadded request identifier are required")
        if self.api_feed_evidence_status == "PUBLIC_DOCUMENTED" and not self.api_feed_evidence_reference:
            raise ValueError("Public documented API/feed evidence requires a reference URL")
        if self.integration_issue is not None and self.state != "INTEGRATION_QUEUED":
            raise ValueError("An existing provider issue may only be linked while queueing integration")
        return self


class SourceCandidateCaptureInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=64)
    discovery_origin: str | None = Field(default=None, max_length=253)
    source_url: str | None = Field(default=None, max_length=2000)
    application_url: str | None = Field(default=None, max_length=2000)
    observed_hops: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def bounded_source_report(self):
        if (not self.profile_id.strip() or not self.request_id.strip()
                or self.request_id != self.request_id.strip()
                or not (self.source_url or self.application_url)):
            raise ValueError("Profile, request identifier, and at least one source URL are required")
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


class ApplicationAnswerStoryFactProvenanceInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    fact_id: str = Field(min_length=1, max_length=128)
    fingerprint: str = Field(min_length=64, max_length=64)


class ApplicationAnswerStoryCitationInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    item_index: int = Field(ge=0, le=99)
    story_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=2147483647)
    fact_provenance: list[ApplicationAnswerStoryFactProvenanceInput] = Field(
        min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_fact_references(self):
        if (not self.story_id.strip()
                or len({fact.fact_id for fact in self.fact_provenance}) != len(self.fact_provenance)
                or any(not fact.fact_id.strip() for fact in self.fact_provenance)):
            raise ValueError("APPLICATION_ANSWER_STORY_CITATION_INVALID")
        return self


class ApplicationAnswerExportInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128)
    items: list[ApplicationAnswerItem] = Field(min_length=1, max_length=100)
    citations: list[ApplicationAnswerStoryCitationInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_story_citations(self):
        identities = [(row.item_index, row.story_id, row.revision) for row in self.citations]
        if len(set(identities)) != len(identities):
            raise ValueError("APPLICATION_ANSWER_STORY_CITATION_INVALID")
        return self


class SensitiveAnswerSuggestionInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    question: str = Field(min_length=1, max_length=4000)


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
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    discovery_origin: str | None = Field(default=None, max_length=253)
    observed_hops: list[str] | None = Field(default=None, max_length=20)
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


class CompanyNoteInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def bounded_note_and_request_id(self):
        if not self.text.strip():
            raise ValueError("Company note must contain 1 to 4000 characters")
        if self.request_id != self.request_id.strip():
            raise ValueError("Tracking request identifier cannot have surrounding whitespace")
        return self


class CompanyContextInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    source_url: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def bounded_company_context(self):
        if not self.text.strip() or self.text != self.text.strip():
            raise ValueError("Company context must contain 1 to 4000 trimmed characters")
        if self.request_id != self.request_id.strip():
            raise ValueError("Company context request identifier cannot have surrounding whitespace")
        return self


class CompanyContextReviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    approved: bool
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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
    request_id: str | None = Field(default=None, min_length=1, max_length=128,
                                   pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


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
    confirm_company_identity: bool = False


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
    provider: Literal["greenhouse", "remotive", "remoteok", "wwr"] = "greenhouse"
    tenant: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    poll_interval: int = Field(default=300, ge=300, le=604800)
    concurrency: int = Field(default=1, ge=1, le=8)
    request_timeout: int = Field(default=10, ge=2, le=60)
    rate_limit: int = Field(default=60, ge=1, le=600)
    jitter_seconds: int = Field(default=30, ge=0, le=300)

    @model_validator(mode="after")
    def global_sources_use_canonical_tenant(self):
        if self.provider != "greenhouse" and self.tenant != "default":
            raise ValueError("Global sources use the canonical default tenant")
        return self


class SearchProfileInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    profile_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    job_filters: dict[str, object] | None = None
    scheduler: dict[str, object] | None = None
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
    from apps.api.search_history import create_search_history_router
    app.include_router(create_search_history_router(store))
    from apps.api.resume_workspace_editor import create_resume_workspace_editor_router
    app.include_router(create_resume_workspace_editor_router(store))
    from apps.api.job_risk_signals import create_job_risk_signals_router
    app.include_router(create_job_risk_signals_router(store))
    from apps.api.recruiter_classification import create_recruiter_classification_router
    app.include_router(create_recruiter_classification_router(store))
    from apps.api.application_priority import create_application_priority_router
    app.include_router(create_application_priority_router(store))
    from apps.api.application_multipage_state import create_application_multipage_state_router
    app.include_router(create_application_multipage_state_router(store))
    from apps.api.application_supporting_documents import create_application_supporting_documents_router
    app.include_router(create_application_supporting_documents_router(store))
    from apps.api.application_attempt_history import create_application_attempt_history_router
    app.include_router(create_application_attempt_history_router(store))
    from apps.api.application_confirmation_recovery import create_application_confirmation_recovery_router
    app.include_router(create_application_confirmation_recovery_router(store, mock_origin, operation_mode))
    from apps.api.application_form_review import create_application_form_review_router
    app.include_router(create_application_form_review_router(store))
    from apps.api.controlled_application_dry_run import create_controlled_application_dry_run_router
    app.include_router(create_controlled_application_dry_run_router(store))
    from apps.api.dynamic_application_fields import create_dynamic_application_fields_router
    app.include_router(create_dynamic_application_fields_router(store))
    from apps.api.document_archive import create_document_archive_router
    app.include_router(create_document_archive_router(store))
    from apps.api.manual_auth import create_manual_auth_router
    app.include_router(create_manual_auth_router(store, mock_origin, output_dir, operation_mode))
    from apps.api.company_discovery_activity import create_company_discovery_activity_router
    app.include_router(create_company_discovery_activity_router(store))
    from apps.api.company_discovery_queue import create_company_discovery_queue_router
    app.include_router(create_company_discovery_queue_router(store))
    from apps.api.skill_gap_heatmap import create_skill_gap_heatmap_router
    app.include_router(create_skill_gap_heatmap_router(store))
    from apps.api.search_profile_suggestions import create_search_profile_suggestions_router
    app.include_router(create_search_profile_suggestions_router(store))
    from apps.api.cheap_prefilter import create_cheap_prefilter_router
    app.include_router(create_cheap_prefilter_router(store))
    from apps.api.resume_page_fit import create_resume_page_fit_router
    app.include_router(create_resume_page_fit_router(store, output_dir))
    from apps.api.resume_rewrite_review import create_resume_rewrite_review_router
    app.include_router(create_resume_rewrite_review_router(store))
    from apps.api.career_story_bank import create_career_story_bank_router
    app.include_router(create_career_story_bank_router(store))
    from apps.api.career_story_selection import create_career_story_selection_router
    app.include_router(create_career_story_selection_router(store))
    from apps.api.cover_letter_preview import create_cover_letter_preview_router
    app.include_router(create_cover_letter_preview_router(store))
    from apps.api.interview_preparation import create_interview_preparation_router
    app.include_router(create_interview_preparation_router(store))
    from apps.api.role_lenses import create_role_lenses_router
    app.include_router(create_role_lenses_router(store))
    from apps.api.browser_job_extraction import create_browser_job_extraction_router
    app.include_router(create_browser_job_extraction_router(store))
    from apps.api.application_answer_matching import create_application_answer_matching_router
    app.include_router(create_application_answer_matching_router(store))
    from apps.api.scoring_profiles import create_scoring_profiles_router
    app.include_router(create_scoring_profiles_router(store))
    from apps.api.matching_explanations import create_matching_explanations_router
    app.include_router(create_matching_explanations_router(store))
    from apps.api.job_comparison import create_job_comparison_router
    app.include_router(create_job_comparison_router(store))
    from apps.api.match_feedback import create_match_feedback_router
    app.include_router(create_match_feedback_router(store))
    from apps.api.company_preferences import create_company_preferences_router
    app.include_router(create_company_preferences_router(store))
    from apps.api.source_relationship_graph import create_source_relationship_graph_router
    app.include_router(create_source_relationship_graph_router(store))
    from apps.api.data_export import create_data_export_router
    app.include_router(create_data_export_router(store, output_dir=output_dir))
    from apps.api.transient_retention import create_transient_retention_router
    app.include_router(create_transient_retention_router(store=store))
    from apps.api.operation_progress import create_operation_progress_router
    app.include_router(create_operation_progress_router(store))
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
        # Pydantic's default response includes each rejected value under
        # `input`. Validation failures can contain credentials, application
        # answers, or other private user data, so retain only structural
        # diagnostics that cannot echo request content.
        safe_errors = []
        for item in error.errors():
            location = item.get("loc", ())
            safe_errors.append({
                "loc": [str(part) for part in location],
                "msg": str(item.get("msg", "Invalid request"))[:240],
                "type": str(item.get("type", "value_error"))[:96],
            })
        return JSONResponse({"detail": safe_errors}, status_code=422)

    @app.exception_handler(NoResultFound)
    async def missing(_, error):
        return JSONResponse({"detail": "Entity not found"}, status_code=404)

    @app.get("/health")
    def health():
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "check": "liveness", "mode": "LOCAL_FIRST", "operation_mode": operation_mode, "external_submission_enabled": False}

    expected_schema_heads = None

    @app.get("/ready")
    @app.get("/readiness")
    def readiness():
        nonlocal expected_schema_heads
        from services.runtime_health import read_runtime_health, REQUIRED_QUEUES
        checks = {"configuration": False, "database": False, "queue": False,
                  "worker": False, "scheduler": False,
                  "workers": {queue: False for queue in REQUIRED_QUEUES}}
        if store.engine.dialect.name == "postgresql":
            checks["schema"] = False
        if operation_mode == "FIXTURE":
            checks["mock"] = False
        try:
            store.assert_workspace_mode(operation_mode)
            checks["configuration"] = True
            with store.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                checks["database"] = True
                if store.engine.dialect.name == "postgresql":
                    from alembic.config import Config
                    from alembic.script import ScriptDirectory
                    from alembic.runtime.migration import MigrationContext
                    if expected_schema_heads is None:
                        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
                        expected_schema_heads = set(ScriptDirectory.from_config(config).get_heads())
                    checks["schema"] = set(MigrationContext.configure(connection).get_current_heads()) == expected_schema_heads
        except Exception:
            pass
        redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            checks["queue"] = bool(redis.ping())
            runtime = read_runtime_health(redis)
            checks["workers"] = runtime["workers"]
            checks["worker"] = all(runtime["workers"].values())
            checks["scheduler"] = runtime["scheduler"]
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
        ready = all(value for key, value in checks.items() if key != "workers")
        return JSONResponse({"status": "ready" if ready else "degraded", "checks": checks,
                             "db_backend": store.engine.dialect.name}, status_code=200 if ready else 503)

    def queued(kind, entity_id, params, *, connection=None):
        operation_id = enqueue_operation(store, kind, entity_id, params, connection=connection)
        if connection is None:
            with store.engine.connect() as owned_connection:
                state = owned_connection.execute(select(m.operations.c.state).where(
                    m.operations.c.id == operation_id)).scalar_one()
        else:
            state = connection.execute(select(m.operations.c.state).where(
                m.operations.c.id == operation_id)).scalar_one()
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

    @app.get("/profiles/{profile_id}/audit-history")
    def profile_audit_history(profile_id: str, limit: int = Query(default=100, ge=1, le=200)):
        try:
            return list_audit_history(store, profile_id, limit)
        except NoResultFound as exc:
            raise HTTPException(404, "Profile not found") from exc

    @app.get("/profiles/{profile_id}/ai-task-settings")
    def ai_task_settings(profile_id: str):
        configured = store.ai_task_settings(profile_id)
        configuration = AITaskConfiguration.from_mapping(configured)
        return {"profile_id": profile_id, "tasks": {
            task: {"provider": configuration.for_task(task).provider,
                   "model": configuration.for_task(task).model}
            for task in AI_TASKS
        }, "fallback_capabilities": fallback_capabilities()}

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
        return {"profile_id": profile_id, "tasks": saved,
                "fallback_capabilities": fallback_capabilities()}

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

    @app.get("/profiles/{profile_id}/application-answer-confidence-thresholds")
    def application_answer_confidence_thresholds(profile_id: str):
        try:
            policy = get_answer_confidence_policy(store, profile_id)
        except NoResultFound as exc:
            raise HTTPException(404, "Profile not found") from exc
        return {"profile_id": profile_id, "policy": policy or DEFAULT_ANSWER_CONFIDENCE_POLICY}

    @app.put("/profiles/{profile_id}/application-answer-confidence-thresholds")
    def save_application_answer_confidence_thresholds(
        profile_id: str, value: AnswerConfidencePolicyInput,
    ):
        try:
            policy = save_answer_confidence_policy(store, profile_id, value.model_dump())
        except NoResultFound as exc:
            raise HTTPException(404, "Profile not found") from exc
        except ValueError as exc:
            raise HTTPException(422, {"code": str(exc)}) from None
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

    @app.post('/profiles/{profile_id}/ai-cost-plan/evaluate/queued', status_code=202)
    def queue_ai_cost_evaluation(profile_id: str, value: AIEvaluationRequest):
        from services.ai.durable_evaluation import queue_evaluation, DurableEvaluationError
        try:
            return queue_evaluation(store, profile_id, value.request_id)
        except DurableEvaluationError as error:
            status_code = 409 if error.code in ('AI_PROVIDER_DISABLED', 'AI_EVALUATION_IDENTITY_CONFLICT') else 422
            raise HTTPException(status_code, {'code': error.code}) from None

    @app.get('/profiles/{profile_id}/ai-cost-plan/operations/{operation_id}')
    def ai_cost_evaluation_status(profile_id: str, operation_id: str):
        from services.ai.durable_evaluation import read_evaluation, DurableEvaluationError
        try:
            return read_evaluation(store, profile_id, operation_id)
        except DurableEvaluationError as error:
            raise HTTPException(404, {'code': error.code}) from None

    @app.get('/profiles/{profile_id}/ai-cost-plan/operations')
    def ai_cost_evaluation_operations(profile_id: str):
        from services.ai.durable_evaluation import KIND, status
        store.profile(profile_id)
        with store.engine.connect() as connection:
            rows = connection.execute(select(m.operations).where(
                m.operations.c.kind == KIND, m.operations.c.entity_id == profile_id,
            ).order_by(m.operations.c.updated_at.desc(), m.operations.c.id.desc()).limit(50)).mappings()
            return {'profile_id': profile_id, 'operations': [status(row) for row in rows]}

    @app.post('/profiles/{profile_id}/ai-cost-plan/operations/{operation_id}/cancel')
    def cancel_queued_ai_cost_evaluation(profile_id: str, operation_id: str):
        from services.ai.durable_evaluation import owned_operation, status, DurableEvaluationError
        from workers.operations import request_cancel
        try:
            owned_operation(store, profile_id, operation_id)
            return status(request_cancel(store, operation_id))
        except DurableEvaluationError as error:
            raise HTTPException(404, {'code': error.code}) from None

    @app.post("/profiles/{profile_id}/ai-cost-plan/evaluate", status_code=202)
    def evaluate_ai_cost_plan(profile_id: str, value: AIEvaluationRequest | None = None):
        # Preserve the original action URL while moving its contract to durable
        # admission. Clients follow the returned operation ID instead of holding
        # an inference request open. No provider executes in the API process.
        return queue_ai_cost_evaluation(profile_id, value or AIEvaluationRequest(request_id=uuid4().hex))

    @app.post("/profiles/{profile_id}/ai-cost-plan/evaluations/{request_id}/cancel")
    def cancel_ai_cost_evaluation(profile_id: str, request_id: str):
        from services.ai.durable_evaluation import KIND, owned_operation, DurableEvaluationError
        from workers.operations import request_cancel
        operation_id = identity(KIND, profile_id, request_id)
        try:
            owned_operation(store, profile_id, operation_id)
        except DurableEvaluationError:
            return {'cancelled': False}
        row = request_cancel(store, operation_id)
        return {'cancelled': row['cancel_requested'], 'operation_id': operation_id, 'state': row['state']}

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
    def profile_document_library(profile_id: str, include_archived: bool = Query(default=False)):
        from services.documents.library import document_library
        from apps.api.document_archive import document_archive_metadata
        result = document_library(store, profile_id)
        for row in result['uploaded']:
            row.update(document_archive_metadata(store, profile_id, None, row['revision_id']))
        for row in result['generated']:
            row.update(document_archive_metadata(store, profile_id, row['application_id'], row['id']))
        if not include_archived:
            result['uploaded'] = [row for row in result['uploaded'] if not row['archived']]
            result['generated'] = [row for row in result['generated'] if not row['archived']]
        return result

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

    @app.post('/profiles/{profile_id}/resumes/{resume_id}/extract', status_code=202)
    def profile_resume_extract(profile_id: str, resume_id: str, value: ResumeExtractionQueueInput | None = None):
        # Original clients now receive durable admission, never parser work bound
        # to the request lifetime. Explicit intent IDs make lost replies replayable.
        return profile_resume_extract_queued(profile_id, resume_id,
            value or ResumeExtractionQueueInput(request_id=uuid4().hex))

    @app.post('/profiles/{profile_id}/resumes/{resume_id}/extract/queued', status_code=202)
    def profile_resume_extract_queued(profile_id: str, resume_id: str, value: ResumeExtractionQueueInput):
        try:
            record, _ = read_resume(store, output_dir, profile_id, resume_id)
        except NoResultFound:
            raise HTTPException(404, "Resume not found") from None
        except UploadIntegrityError as error:
            raise HTTPException(409, str(error)) from None
        try:
            return queue_durable_extraction(store, profile_id, record, value.request_id)
        except DurableExtractionError as error:
            status = 409 if error.code.endswith("CONFLICT") else 422
            raise HTTPException(status, {"code": error.code}) from None

    def durable_resume_extraction_read(profile_id: str, resume_id: str, operation_id=None):
        try:
            return read_durable_extraction(store, output_dir, profile_id, resume_id, operation_id)
        except DurableExtractionError as error:
            status = 404 if error.code == "RESUME_EXTRACTION_NOT_FOUND" else 409
            raise HTTPException(status, {"code": error.code}) from None

    @app.get('/profiles/{profile_id}/resumes/{resume_id}/extract/queued')
    def profile_resume_extract_latest(profile_id: str, resume_id: str):
        return durable_resume_extraction_read(profile_id, resume_id)

    @app.get('/profiles/{profile_id}/resumes/{resume_id}/extract/queued/{operation_id}')
    def profile_resume_extract_result(profile_id: str, resume_id: str, operation_id: str):
        return durable_resume_extraction_read(profile_id, resume_id, operation_id)

    @app.post('/profiles/{profile_id}/resumes/{resume_id}/extract/cancel')
    def profile_resume_extract_cancel(profile_id: str, resume_id: str):
        if not any(resume['id'] == resume_id for resume in list_resumes(store, profile_id)):
            raise HTTPException(404, "Resume not found")
        with store.engine.connect() as connection:
            active_operations = list(connection.execute(select(
                m.operations.c.id, m.operations.c.cancel_requested,
            ).where(
                m.operations.c.kind.in_(("RESUME_EXTRACT", DURABLE_RESUME_EXTRACT_KIND)),
                m.operations.c.entity_id == resume_id,
                m.operations.c.state.in_(("QUEUED", "RUNNING")),
            )).mappings())
        durable_cancelled = False
        for operation in active_operations:
            if operation["cancel_requested"]:
                durable_cancelled = True
                continue
            result = request_cancel(store, operation["id"])
            durable_cancelled = durable_cancelled or bool(result.get("cancel_requested"))
        return {"cancelled": durable_cancelled}

    @app.post("/discovery", status_code=202)
    def discovery(value: DiscoveryInput):
        if operation_mode != "FIXTURE":
            raise HTTPException(409, "Fictional discovery is available only in verification mode")
        profile = store.profile(value.profile_id)
        params = {"profile_version": profile["version"], "request_id": value.request_id}
        if value.search_profile_id:
            try:
                search = get_search_profile(store, value.search_profile_id)
            except NoResultFound as exc:
                raise HTTPException(404, "Search profile not found") from exc
            if search["profile_id"] != value.profile_id:
                raise HTTPException(404, "Search profile not found")
            params["search_profile_id"] = value.search_profile_id
        if value.query is not None:
            params["query"] = value.query
        if value.filters is not None:
            params["filters"] = value.filters
        return queued("DISCOVER", value.profile_id, params)

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
        from services.company_discovery.company_context import list_company_contexts
        rows = list_company_source_view(store, profile_id)
        for row in rows:
            row["company_contexts"] = list_company_contexts(store, row["id"])
        return {"company_sources": rows}

    @app.post("/company-sources/{source_id}/contexts", status_code=201)
    def company_context_save(source_id: str, value: CompanyContextInput):
        from services.company_discovery.company_context import save_company_context
        try:
            return save_company_context(store, source_id, **value.model_dump())
        except ValueError as exc:
            code = str(exc)
            status = 404 if code == "COMPANY_CONTEXT_SOURCE_NOT_FOUND" else (
                409 if code == "COMPANY_CONTEXT_REQUEST_CONFLICT" else 422)
            raise HTTPException(status, code) from exc

    @app.post("/company-sources/{source_id}/contexts/{context_id}/review")
    def company_context_review(source_id: str, context_id: str,
                               value: CompanyContextReviewInput):
        from services.company_discovery.company_context import review_company_context
        try:
            return review_company_context(store, context_id, company_source_id=source_id,
                                          **value.model_dump())
        except ValueError as exc:
            code = str(exc)
            status = 404 if code == "COMPANY_CONTEXT_NOT_FOUND" else (
                409 if code in {"COMPANY_CONTEXT_SOURCE_UNVERIFIED",
                                "COMPANY_CONTEXT_CONTENT_CHANGED"} else 422)
            raise HTTPException(status, code) from exc

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

    @app.get("/search-skills")
    def search_skills():
        return {"skills": [{"id": identifier, "label": entry[0]}
                           for identifier, entry in SKILL_TAXONOMY.items()]}

    @app.get("/search-profiles/{search_profile_id}")
    def search_profile_read(search_profile_id: str):
        return get_search_profile(store, search_profile_id)

    @app.put("/search-profiles")
    def search_profile_save(value: SearchProfileInput):
        try:
            # Keep omitted fields omitted: saved-search cadence updates are partial,
            # and must not reset a profile's existing filters or scheduler settings.
            return {"id": save_search_profile(store, value.model_dump(exclude_unset=True))}
        except ValueError as error:
            if str(error) == "Search profile belongs to a different career profile":
                raise HTTPException(409, str(error)) from error
            raise HTTPException(422, str(error)) from error

    @app.get("/profiles/{profile_id}/settings/export")
    def profile_settings_export(profile_id: str):
        from services.settings_transfer import export_settings
        try:
            return export_settings(store, profile_id)
        except ValueError as error:
            if str(error) == "Career profile does not exist":
                raise HTTPException(404, "Career profile not found") from None
            raise HTTPException(422, "Settings export is unavailable") from None

    @app.post("/profiles/{profile_id}/settings/import")
    def profile_settings_import(profile_id: str, payload: dict):
        from services.settings_transfer import import_settings
        try:
            return import_settings(store, profile_id, payload)
        except ValueError as error:
            if str(error) == "Career profile does not exist":
                raise HTTPException(404, "Career profile not found") from None
            raise HTTPException(422, "Settings document is invalid or unsupported") from None

    @app.put("/sources")
    def source_configure(value: SourceInput):
        return {"id": configure_source(store, **value.model_dump())}

    @app.post("/sources/{source_id}/collect", status_code=202)
    def source_collect(source_id: str, value: DiscoveryInput):
        profile = store.profile(value.profile_id)
        with store.engine.connect() as connection:
            connection.execute(select(m.job_sources.c.id).where(m.job_sources.c.id == source_id)).one()
            saved_search = None
            if value.search_profile_id:
                saved_search = connection.execute(select(m.search_profiles).where(
                    m.search_profiles.c.id == value.search_profile_id
                )).mappings().one_or_none()
        if value.search_profile_id:
            if saved_search is None:
                raise HTTPException(404, "Saved search not found")
            if saved_search["profile_id"] != value.profile_id:
                raise HTTPException(409, "Saved search belongs to another profile")
            if not saved_search["enabled"]:
                raise HTTPException(409, "Saved search is disabled")
            if saved_search["source_ids"] and source_id not in saved_search["source_ids"]:
                raise HTTPException(409, "Saved search is not configured for this source")
        params = {"source_id": source_id, "profile_version": profile["version"], "request_id": value.request_id}
        if value.search_profile_id:
            params["search_profile_id"] = value.search_profile_id
        return queued("DISCOVER", value.profile_id, params)

    @app.post("/jobs/manual")
    def manual_job_capture(value: ManualJobCaptureInput, response: Response):
        from services.discovery.manual_capture import ManualCaptureConflict, capture_manual_job
        try:
            result = capture_manual_job(store, value.model_dump(mode="json"))
        except ManualCaptureConflict as error:
            raise HTTPException(409, str(error)) from error
        except NoResultFound:
            raise HTTPException(404, "Career profile not found") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        response.status_code = 201 if result["created"] else 200
        return result

    @app.get("/source-candidates")
    def source_candidates(profile_id: str = Query(min_length=1, max_length=128)):
        from services.source_candidates.catalogue import list_candidates
        try:
            return list_candidates(store, profile_id)
        except NoResultFound:
            raise HTTPException(404, "Career profile not found") from None

    @app.post("/source-candidates")
    def source_candidate_capture(value: SourceCandidateCaptureInput, response: Response):
        from services.source_candidates.catalogue import SourceCandidateConflict, report_source_candidate
        try:
            result = report_source_candidate(
                store, profile_id=value.profile_id, request_id=value.request_id,
                discovery_origin=value.discovery_origin, source_url=value.source_url,
                application_url=value.application_url, observed_hops=value.observed_hops,
            )
        except NoResultFound:
            raise HTTPException(404, "Career profile not found") from None
        except SourceCandidateConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        response.status_code = 201 if result["created"] else 200
        return result

    @app.put("/profiles/{profile_id}/source-candidates/{candidate_id}/review")
    def source_candidate_review(profile_id: str, candidate_id: str,
                                value: SourceCandidateReviewInput):
        from services.source_candidates.catalogue import SourceCandidateConflict, review_candidate
        try:
            return review_candidate(store, profile_id, candidate_id,
                                    value.state, value.site_type, value.reason, value.request_id,
                                    value.api_feed_evidence_status, value.api_feed_evidence_reference,
                                    value.api_feed_evidence_note, value.access_terms_note,
                                    value.capabilities_limits_note,
                                    value.expected_value_maintenance_note,
                                    value.integration_issue)
        except NoResultFound:
            raise HTTPException(404, "Source candidate not found") from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/jobs")
    def jobs(profile_id: str | None = None, q: str = Query(default="", max_length=200),
             title: str = Query(default="", max_length=200),
             company: str = Query(default="", max_length=200),
             location: str = Query(default="", max_length=200),
             remote_type: str | None = Query(default=None, max_length=32),
             provider: Literal["mock-ats", "greenhouse"] | None = None,
             ats_provider: Literal["mock-ats", "greenhouse"] | None = None,
             sort: Literal["score", "newest", "company", "title", "salary_high", "salary_low",
                           "application_priority"] = "score",
             salary_min: float | None = Query(default=None, ge=0, le=100000000),
             salary_max: float | None = Query(default=None, ge=0, le=100000000),
             min_match_score: int | None = Query(default=None, ge=0, le=100),
             experience_level: Literal["INTERNSHIP", "ENTRY", "JUNIOR", "ASSOCIATE", "MID", "SENIOR",
                                       "LEAD", "MANAGER", "DIRECTOR", "EXECUTIVE", "UNKNOWN"] | None = None,
             employment_type: Literal["FULL_TIME", "PART_TIME", "CONTRACT", "CONTRACT_TO_HIRE",
                                     "TEMPORARY", "INTERNSHIP", "OTHER", "UNKNOWN"] | None = None,
             skill: str | None = Query(default=None, max_length=100),
             education_requirement: str | None = Query(default=None, max_length=64),
             clearance_level: str | None = Query(default=None, max_length=32),
             job_source_id: str | None = Query(default=None, max_length=128),
             application_link_status: Literal["", "AVAILABLE", "UNAVAILABLE"] | None = Query(default=None, max_length=16),
             application_pipeline_status: str | None = Query(default=None, max_length=32),
             application_capability: Literal["", "FIXTURE_ONLY", "UNSUPPORTED", "REVIEW_REQUIRED", "UNKNOWN"] | None = Query(default=None, max_length=24),
             posted_within_days: int | None = Query(default=None, ge=1, le=30),
             override_saved_match_score: bool = False,
             search_profile_id: str | None = Query(default=None, max_length=128),
             include_dismissed: bool = False,
             include_blacklisted: bool = False,
             limit: int = Query(default=200, ge=1, le=500)):
        if salary_min is not None and salary_max is not None and salary_min > salary_max:
            raise HTTPException(422, "Minimum annual USD salary cannot exceed maximum annual USD salary")
        if posted_within_days is not None and posted_within_days not in {1, 3, 7, 14, 30}:
            raise HTTPException(422, "Posted-within window must be 1, 3, 7, 14, or 30 days")
        skill_id = canonical_skill_id(skill) if skill else None
        if skill and skill_id is None:
            raise HTTPException(422, "Skill is not in the supported taxonomy")
        if education_requirement is not None:
            education_requirement = education_requirement.strip().upper()
            if not education_requirement or education_requirement not in EDUCATION_REQUIREMENTS:
                raise HTTPException(422, "Education requirement is not supported")
        if clearance_level is not None:
            clearance_level = clearance_level.strip().upper()
            if not clearance_level or clearance_level not in CLEARANCE_LEVELS:
                raise HTTPException(422, "Clearance requirement is not supported")
        if job_source_id is not None:
            job_source_id = job_source_id.strip()
            if not job_source_id:
                job_source_id = None
            else:
                with store.engine.connect() as connection:
                    known_source = connection.execute(select(m.job_sources.c.id).where(
                        m.job_sources.c.id == job_source_id)).scalar_one_or_none()
                if known_source is None:
                    raise HTTPException(422, "Job source is not configured")
        if (application_pipeline_status is not None
                and application_pipeline_status not in {"", *PIPELINE_STATUSES, "NOT_TRACKED"}):
            raise HTTPException(422, "Application pipeline status is not supported")
        if sort == "application_priority" and not profile_id:
            raise HTTPException(422, "Application-priority sorting requires a career profile")
        search_profile = get_search_profile(store, search_profile_id) if search_profile_id else None
        if search_profile and search_profile["profile_id"] != profile_id:
            raise HTTPException(409, "Search profile belongs to a different career profile")
        saved_score = ((search_profile["criteria"].get("job_filters") or {}).get("min_match_score", "")
                       if search_profile else "")
        if override_saved_match_score:
            effective_match_score = min_match_score
        elif saved_score:
            effective_match_score = max(min_match_score or 0, int(saved_score))
        else:
            effective_match_score = min_match_score
        saved_posted_within = ((search_profile["criteria"].get("job_filters") or {}).get("posted_within_days", "")
                               if search_profile else "")
        saved_application_link_status = ((search_profile["criteria"].get("job_filters") or {}).get("application_link_status")
                                         if search_profile else None)
        effective_application_link_status = (application_link_status if application_link_status is not None
                                             else saved_application_link_status)
        if effective_application_link_status == "":
            effective_application_link_status = None
        saved_application_pipeline_status = (
            (search_profile["criteria"].get("job_filters") or {}).get("application_pipeline_status")
            if search_profile else None
        )
        effective_application_pipeline_status = (
            application_pipeline_status if application_pipeline_status is not None
            else saved_application_pipeline_status
        )
        if effective_application_pipeline_status == "":
            effective_application_pipeline_status = None
        if effective_application_pipeline_status is not None and not profile_id:
            raise HTTPException(422, "Application pipeline filtering requires a career profile")
        saved_application_capability = (
            (search_profile["criteria"].get("job_filters") or {}).get("application_capability")
            if search_profile else None
        )
        effective_application_capability = (
            application_capability if application_capability is not None else saved_application_capability
        )
        if effective_application_capability == "":
            effective_application_capability = None
        effective_posted_within = posted_within_days if posted_within_days is not None else (
            int(saved_posted_within) if saved_posted_within else None)
        posted_before = datetime.now(timezone.utc) if effective_posted_within is not None else None
        posted_after = (posted_before - timedelta(days=effective_posted_within)
                        if posted_before is not None else None)
        result_limit = 500 if sort == "application_priority" else limit
        database_sort = "score" if sort == "application_priority" else sort
        excluded_company_ids = []
        if profile_id and not include_blacklisted:
            from services.matching.company_preferences import list_company_preferences
            try:
                excluded_company_ids = [
                    item["company_id"]
                    for item in list_company_preferences(store, profile_id)
                    if item["status"] == "BLACKLISTED"
                ]
            except LookupError:
                raise HTTPException(404, "Career profile not found") from None
        rows = store.jobs(profile_id, q, remote_type, provider, database_sort, result_limit,
                          title=title, company=company, location=location, ats_provider=ats_provider,
                          salary_min=salary_min, salary_max=salary_max,
                          min_match_score=effective_match_score, experience_level=experience_level,
                          employment_type=employment_type,
                          skill_id=skill_id,
                          education_requirement=education_requirement,
                          clearance_level=clearance_level,
                          job_source_id=job_source_id,
                          application_link_status=effective_application_link_status,
                          application_pipeline_status=effective_application_pipeline_status,
                          application_capability=effective_application_capability,
                          posted_after=posted_after, posted_before=posted_before,
                          exclude_company_ids=excluded_company_ids)
        if search_profile_id:
            rows = filter_jobs(rows, search_profile["criteria"])
        if profile_id:
            from services.tracking.jobs import list_job_tracking
            tracking = list_job_tracking(store, profile_id, [row["id"] for row in rows])
            for row in rows:
                row["tracking"] = tracking[row["id"]]
            if not include_dismissed:
                rows = [row for row in rows if row["tracking"]["disposition"] not in ("HIDDEN", "NOT_INTERESTED")]
        if sort == "application_priority":
            from apps.api.application_priority import rank_jobs_by_application_priority
            try:
                rows = rank_jobs_by_application_priority(store, profile_id, rows)
            except (LookupError, TypeError, ValueError):
                raise HTTPException(502, "Application-priority ranking is temporarily unavailable") from None
            rows = rows[:limit]
        if rows:
            job_ids = [row["id"] for row in rows]
            with store.engine.connect() as connection:
                source_rows = connection.execute(
                    select(m.postings.c.job_id, m.postings.c.source)
                    .where(m.postings.c.job_id.in_(job_ids))
                    .distinct()
                    .order_by(m.postings.c.job_id, m.postings.c.source)
                ).all()
            sources_by_job = {}
            for job_id, source in source_rows:
                sources_by_job.setdefault(job_id, []).append(source)
            for row in rows:
                row["posting_sources"] = sources_by_job.get(row["id"], [])
        return {"jobs": rows}

    @app.get("/jobs/{job_id}/routes")
    def job_routes(job_id: str, profile_id: str | None = Query(default=None, min_length=1, max_length=128)):
        routes = load_job_routes(store, job_id, profile_id)
        with store.engine.connect() as connection:
            for route in routes:
                route["adapter_knowledge"] = application_adapter_knowledge(connection, route["id"], operation_mode=operation_mode)
        plan = plan_application_routes(routes, "ALL_DISTINCT")
        with store.engine.connect() as connection:
            postings = [dict(row) for row in connection.execute(select(m.postings).where(m.postings.c.job_id == job_id).order_by(m.postings.c.id)).mappings()]
        return {**plan, "source_records": source_provenance_inventory(postings)}

    @app.put("/profiles/{profile_id}/companies/{company_id}/preferred-application-route")
    def preferred_application_route(profile_id: str, company_id: str,
                                    value: PreferredApplicationRouteInput):
        from services.dedupe.route_trust import set_preferred_application_route
        try:
            return {"preferred_route": set_preferred_application_route(
                store, profile_id, company_id, value.route_id,
                value.preferred, value.request_id,
            )}
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            code = str(error)
            raise HTTPException(409 if code.endswith("CONFLICT") else 422, code) from None

    @app.post("/jobs/{job_id}/routes/{route_id}/resolve")
    def resolve_job_application_route(job_id: str, route_id: str):
        """Explicitly check one saved aggregator URL for a verified employer/ATS redirect."""
        with store.engine.connect() as connection:
            source = connection.execute(
                select(m.routes).where(m.routes.c.job_id == job_id, m.routes.c.id == route_id)
            ).mappings().first()
            if source is None:
                raise HTTPException(404, "Saved application route not found")
            source = dict(source)
            if route_authority(source) != "AGGREGATOR":
                raise HTTPException(409, "Only an aggregator route can be checked")
            domains = connection.execute(
                select(m.company_domains.c.domain).where(
                    m.company_domains.c.company_id == source["company_id"],
                    m.company_domains.c.verified.is_(True),
                ).order_by(m.company_domains.c.domain)
            ).scalars().all()
            tenants = [dict(row) for row in connection.execute(
                select(m.company_ats_tenants).where(
                    m.company_ats_tenants.c.company_id == source["company_id"]
                ).order_by(m.company_ats_tenants.c.id)
            ).mappings()]
        result = resolve_aggregator_redirect(
            source["url"], verified_domains=domains, ats_tenants=tenants,
        )
        return {"source_route_id": route_id, **result}

    @app.post("/jobs/{job_id}/routes/{route_id}/save-verified-destination")
    def save_job_application_route_destination(job_id: str, route_id: str,
                                               response: Response):
        """Recheck and explicitly persist one verified destination as a selectable route."""
        with store.engine.connect() as connection:
            source = connection.execute(
                select(m.routes).where(m.routes.c.job_id == job_id, m.routes.c.id == route_id)
            ).mappings().first()
            if source is None:
                raise HTTPException(404, "Saved application route not found")
            source = dict(source)
            if route_authority(source) != "AGGREGATOR":
                raise HTTPException(409, "Only an aggregator route can be checked")
            domains = connection.execute(
                select(m.company_domains.c.domain).where(
                    m.company_domains.c.company_id == source["company_id"],
                    m.company_domains.c.verified.is_(True),
                ).order_by(m.company_domains.c.domain)
            ).scalars().all()
            tenants = [dict(row) for row in connection.execute(
                select(m.company_ats_tenants).where(
                    m.company_ats_tenants.c.company_id == source["company_id"]
                ).order_by(m.company_ats_tenants.c.id)
            ).mappings()]
        result = resolve_aggregator_redirect(
            source["url"], verified_domains=domains, ats_tenants=tenants,
        )
        try:
            with store.engine.begin() as connection:
                current = connection.execute(
                    select(m.routes).where(
                        m.routes.c.job_id == job_id, m.routes.c.id == route_id,
                    )
                ).mappings().first()
                if (current is None or current["company_id"] != source["company_id"]
                        or current["url"] != source["url"]
                        or route_authority(dict(current)) != "AGGREGATOR"):
                    raise HTTPException(409, "APPLICATION_ROUTE_CHANGED")
                saved = save_verified_redirect_destination(connection, dict(current), result)
                if saved["created"]:
                    store._audit(connection, job_id, "APPLICATION_ROUTE_DESTINATION_SAVED", {
                        "source_route_id": route_id,
                        "destination_route_id": saved["route_id"],
                        "provider": result.get("provider") or "employer",
                        "tenant": result.get("tenant") or "",
                    })
        except ValueError as error:
            code = str(error)
            raise HTTPException(409 if code == "ROUTE_DESTINATION_NOT_VERIFIED" else 422, code) from None
        saved_route = next(
            route for route in load_job_routes(store, job_id)
            if route["id"] == saved["route_id"]
        )
        response.status_code = 201 if saved["created"] else 200
        return {
            "source_route_id": route_id,
            "status": result["status"],
            "created": saved["created"],
            "route": saved_route,
        }

    @app.get("/jobs/{job_id}")
    def job(job_id: str, profile_id: str | None = None):
        result = store.job(job_id, profile_id)
        result["routes"] = load_job_routes(store, job_id, profile_id)
        result['alignment_fingerprint'] = target_fingerprint(result)
        from services.documents.cover_letter_preview import cover_letter_preview_fingerprint
        result["cover_letter_preview_fingerprint"] = cover_letter_preview_fingerprint(result)
        if profile_id:
            from services.tracking.jobs import read_job_tracking
            result["tracking"] = read_job_tracking(store, profile_id, job_id)
        return result

    class ResumeAlignmentPreviewInput(BaseModel):
        model_config = ConfigDict(strict=True, extra='forbid')
        expected_profile_version: str = Field(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$')
        expected_job_fingerprint: str = Field(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$')
        selected_fact_ids: list[Annotated[str, Field(strict=True, min_length=1, max_length=128)]] | None = Field(default=None, max_length=1000)

    @app.post('/profiles/{profile_id}/jobs/{job_id}/resume-alignment-preview')
    def resume_alignment_preview(profile_id: str, job_id: str, value: ResumeAlignmentPreviewInput):
        profile = store.profile(profile_id)
        if profile['version'] != value.expected_profile_version:
            raise HTTPException(409, 'Profile changed; reload approved facts before previewing')
        job = store.job(job_id)
        if job['id'] != job_id:
            raise HTTPException(404, 'Saved target job not found')
        try:
            return build_alignment_preview(profile, job, value.expected_profile_version,
                                           value.expected_job_fingerprint, value.selected_fact_ids)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

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

    @app.get("/profiles/{profile_id}/companies/{company_id}/tracking")
    def company_tracking_read(profile_id: str, company_id: str):
        from services.tracking.companies import read_company_tracking
        try:
            return read_company_tracking(store, profile_id, company_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @app.post("/profiles/{profile_id}/companies/{company_id}/tracking/notes", status_code=201)
    def company_note_create(profile_id: str, company_id: str, value: CompanyNoteInput):
        from services.tracking.companies import add_company_note
        try:
            return add_company_note(store, profile_id, company_id, value.text, value.request_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            status = 409 if "already used for different content" in str(error) else 422
            raise HTTPException(status, str(error)) from None

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
                row["external_reports"] = value.get("external_reports", [])
        return {"applications": rows}

    @app.get("/profiles/{profile_id}/application-follow-up-drafts")
    def application_follow_up_drafts(
            profile_id: str, quiet_days: int = Query(default=7, ge=1, le=90)):
        from services.tracking.follow_up_drafts import draft_follow_up_candidates
        try:
            return draft_follow_up_candidates(store, profile_id, quiet_days)
        except NoResultFound:
            raise HTTPException(404, "Profile or application not found") from None

    @app.post("/applications/external")
    def external_application_create(value: ExternalApplicationInput, response: Response):
        from services.applications.external_capture import (
            ExternalCaptureConflict, capture_external_application,
        )
        try:
            result = capture_external_application(
                store, value.model_dump(mode="json"))
        except ExternalCaptureConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        response.status_code = 201 if result["created"] else 200
        return result

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

    def prepare_application(value: ApplicationInput, *, operation_connection=None):
        from services.dedupe.source_values import require_route_review
        from services.documents.tailor import eligible_rows, select_resume_rows, validate_user_rewrites
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
        if value.expected_job_fingerprint and value.expected_job_fingerprint != target_fingerprint(job):
            raise HTTPException(409, 'Target job changed; reload and preview the saved job before preparing')
        manual_postings = [posting for posting in job.get("postings", [])
                           if posting.get("source") == "manual"]
        if manual_postings and not any(
                (posting.get("raw") or {}).get("submitted_fields", {}).get("application_url")
                for posting in manual_postings):
            raise HTTPException(422, "Add an application URL before preparing this manually captured job")
        try:
            select_resume_rows(eligible_rows(profile),value.tailor_resume,value.tailor_fact_ids)
            if value.tailor_fact_rewrites is not None:
                validate_user_rewrites(eligible_rows(profile), value.tailor_fact_ids,
                    [rewrite.model_dump() for rewrite in value.tailor_fact_rewrites])
        except PreparationError as error:
            raise HTTPException(409,str(error)) from error
        if value.cover_letter_story_ids is not None:
            from services.career_profile.story_selection import StorySelectionError, select_stories_for_job
            try:
                selection = select_stories_for_job(
                    store, value.profile_id, value.job_id, "COVER_LETTER",
                    value.cover_letter_story_ids,
                )
            except StorySelectionError as error:
                raise HTTPException(error.status_code, {"code": error.code}) from None
            if any(story["profile_version"] != profile["version"]
                   for story in selection["stories"]):
                raise HTTPException(409, {"code": "STORY_SELECTION_CONTEXT_STALE"})
        if value.resume_story_ids is not None:
            from services.career_profile.story_selection import StorySelectionError, select_stories_for_job
            try:
                selection = select_stories_for_job(
                    store, value.profile_id, value.job_id, "RESUME",
                    value.resume_story_ids,
                )
            except StorySelectionError as error:
                raise HTTPException(error.status_code, {"code": error.code}) from None
            if any(story["profile_version"] != profile["version"]
                   for story in selection["stories"]):
                raise HTTPException(409, {"code": "STORY_SELECTION_CONTEXT_STALE"})
        with store.engine.connect() as connection:
            connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == value.job_id)).one()
            try:
                require_route_review(connection, value.job_id, value.route_mode, value.route_id)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
        route_preference = (profile.get("preferences") or {}).get(
            "application_route_preference", "PREFER_VERIFIED")
        if route_preference == "USER_CHOICE" and value.route_mode != "THIS_ROUTE":
            raise HTTPException(409, "APPLICATION_ROUTE_SELECTION_REQUIRED")
        routes = load_job_routes(store, value.job_id, value.profile_id)
        try:
            route_plan = plan_application_routes(
                routes, value.route_mode, value.route_id, route_preference)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
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
            **({'cover_letter_story_ids': value.cover_letter_story_ids,
                'cover_letter_story_job_fingerprint': value.expected_job_fingerprint}
               if value.cover_letter_story_ids is not None else {}),
            **({'resume_story_ids': value.resume_story_ids,
                'resume_story_job_fingerprint': value.expected_job_fingerprint}
               if value.resume_story_ids is not None else {}),
            **({'tailor_fact_ids':value.tailor_fact_ids} if value.tailor_fact_ids is not None else {}),
            **({'tailor_fact_rewrites':[rewrite.model_dump() for rewrite in value.tailor_fact_rewrites]} if value.tailor_fact_rewrites is not None else {}),
            **({'role_lens_id': value.role_lens_id,
                'expected_role_lens_version': value.expected_role_lens_version}
               if value.role_lens_id is not None else {}),
        }, connection=operation_connection)
        return {**operation, "application_id": identity(value.job_id, value.profile_id),
                "route_mode": route_plan["mode"], "route_ids": [route["id"] for route in selected_routes]}

    @app.post("/applications", status_code=202)
    def application(value: ApplicationInput):
        return prepare_application(value)

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

    @app.post("/profiles/{profile_id}/applications/{application_id}/sensitive-answer-suggestion")
    def sensitive_answer_suggestion(profile_id: str, application_id: str,
                                    value: SensitiveAnswerSuggestionInput):
        from services.documents.sensitive_answer_policy import (
            classify_sensitive_category,
            resolve_sensitive_answer,
        )

        application = store.application(application_id)
        if application["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        profile = store.profile(profile_id)
        category = classify_sensitive_category(value.question)
        category_keys = {
            "race_ethnicity": "RACE_ETHNICITY",
            "religion": "RELIGION",
            "sex_gender": "SEX_GENDER",
            "disability": "DISABILITY",
            "veteran_status": "VETERAN_STATUS",
            "other_voluntary_demographics": "OTHER_VOLUNTARY_DEMOGRAPHIC",
        }
        profile_category = category_keys.get(category) if category else None
        policies = (profile.get("preferences") or {}).get("sensitive_answer_policies", {})
        stored_policy = policies.get(profile_category) if profile_category else None
        policy = dict(stored_policy) if isinstance(stored_policy, dict) else None
        if policy is not None and policy.get("response") is None:
            policy.pop("response", None)
        proposal = resolve_sensitive_answer(category, {category: policy} if policy else {})
        return {"category": profile_category, **proposal}

    @app.post("/profiles/{profile_id}/applications/{application_id}/answers/export", status_code=201)
    def export_application_answers(profile_id: str, application_id: str, value: ApplicationAnswerExportInput):
        from services.documents.answers import export_answers
        from services.documents.answer_story_citations import (
            AnswerStoryCitationError,
            record_answer_story_citations,
            validate_answer_story_citations,
        )
        application = store.application(application_id)
        if application["profile_id"] != profile_id or value.profile_id != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        try:
            citations = validate_answer_story_citations(
                store, profile_id, value.items, value.citations)
        except AnswerStoryCitationError as error:
            raise HTTPException(422, {"code": error.code}) from None
        record = export_answers(store, application_id, output_dir, value.items)
        record_answer_story_citations(
            store, application_id=application_id, document_id=record["id"],
            document_version=record["version"], citations=citations)
        return {"document_id": record["id"], "version": record["version"],
                "sha256": record["sha256"]["txt"], "external_submission": "OFF",
                "story_citation_count": len(citations)}

    @app.get('/profiles/{profile_id}/applications/{application_id}/document-comparison')
    def document_comparison(profile_id: str, application_id: str, base_document_id: str, tailored_document_id: str):
        from services.documents.comparison import compare_resumes, ComparisonUnavailable
        if store.application(application_id)["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        try:
            return compare_resumes(store, output_dir, application_id, base_document_id, tailored_document_id)
        except ComparisonUnavailable as error:
            raise HTTPException(409, str(error)) from error

    def comparison_error(error):
        from services.documents.durable_comparison import DurableComparisonError
        if isinstance(error, DurableComparisonError):
            raise HTTPException(404 if error.code == 'COMPARISON_NOT_FOUND' else 409,
                                {'code': error.code, 'message': str(error)}) from None
        raise error

    @app.post('/profiles/{profile_id}/applications/{application_id}/uploaded-resume-comparison/operations', status_code=202)
    def queue_uploaded_comparison(profile_id: str, application_id: str, base_revision_id: str,
                                 tailored_document_id: str, request_id: str):
        from services.documents.durable_comparison import queue_comparison, DurableComparisonError
        try:
            admitted = queue_comparison(store, output_dir, profile_id, application_id,
                                       base_revision_id, tailored_document_id, request_id)
        except DurableComparisonError as error:
            comparison_error(error)
        return admitted

    @app.get('/profiles/{profile_id}/applications/{application_id}/uploaded-resume-comparison/operations')
    def read_uploaded_comparison(profile_id: str, application_id: str, base_revision_id: str,
                                tailored_document_id: str, operation_id: str | None = None):
        from services.documents.durable_comparison import read_comparison, DurableComparisonError
        try:
            return read_comparison(store, output_dir, profile_id, application_id,
                                   base_revision_id, tailored_document_id, operation_id)
        except DurableComparisonError as error:
            comparison_error(error)

    @app.post('/profiles/{profile_id}/applications/{application_id}/uploaded-resume-comparison/operations/{operation_id}/cancel')
    def cancel_queued_comparison(profile_id: str, application_id: str, operation_id: str):
        from services.documents.durable_comparison import owned_operation, status, DurableComparisonError
        try:
            owned_operation(store, profile_id, application_id, operation_id)
            return status(request_cancel(store, operation_id))
        except DurableComparisonError as error:
            comparison_error(error)

    @app.get('/profiles/{profile_id}/applications/{application_id}/uploaded-resume-comparison')
    def uploaded_resume_comparison(profile_id: str, application_id: str, base_revision_id: str,
                                   tailored_document_id: str, operation_id: str | None = None):
        # GET is a read: admission is exclusively POST /operations. Reuse the
        # same immutable-input and ownership validation as the durable UI path.
        return read_uploaded_comparison(profile_id, application_id, base_revision_id,
                                        tailored_document_id, operation_id)

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
                m.operations.c.state.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
                ((m.operations.c.kind == "UPLOADED_RESUME_COMPARE") &
                 (m.operations.c.entity_id == entity_id)) |
                ((m.operations.c.kind == "UPLOADED_RESUME_COMPARE_QUEUED") &
                 (m.operations.c.entity_id == application_id) &
                 (m.operations.c.params['profile_id'].as_string() == profile_id) &
                 (m.operations.c.params['base_revision_id'].as_string() == base_revision_id) &
                 (m.operations.c.params['tailored_document_id'].as_string() == tailored_document_id)),
            )).mappings())
        durable_cancelled = False
        for operation in active_operations:
            if operation["cancel_requested"]:
                durable_cancelled = True
            else:
                result = request_cancel(store, operation["id"])
                durable_cancelled = durable_cancelled or bool(result.get("cancel_requested"))
        return {"cancelled": durable_cancelled}

    def serve_document(document, format):
        document = dict(document)
        root = Path(output_dir).resolve()
        if document["kind"] == "resume":
            from services.documents.history import validated_resume_preview
            from services.documents.errors import PreparationError
            try:
                validated_resume_preview(store, document, output_dir)
            except (OSError, ValueError, KeyError, TypeError, PreparationError) as error:
                if getattr(error, 'code', None) == 'AVOID_LIST_BLOCKED':
                    raise HTTPException(409, {"code": error.code, "message": str(error)}) from error
                raise HTTPException(409, "Resume validation failed; revise and prepare a new version") from error
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
        if value.request_id is None:
            # Existing clients retain their original deterministic outbox identity.
            return queued("APPLY_MOCK", application_id, {"submit": value.submit})
        from services.applications.run_requests import queue_mock_run
        from services.applications.attempt_history import AttemptConflict
        try:
            return queue_mock_run(store, profile_id, application_id,
                                  submit=value.submit, request_id=value.request_id)
        except AttemptConflict as error:
            raise HTTPException(409, {"code": error.code}) from None

    @app.get("/profiles/{profile_id}/applications/{application_id}/mock-runs/{request_id}")
    def mock_run_request(profile_id: str, application_id: str, request_id: str):
        if operation_mode != "FIXTURE":
            raise HTTPException(409, "Fictional application actions are available only in verification mode")
        from services.applications.run_requests import read_mock_run
        from services.applications.attempt_history import AttemptConflict
        try:
            return read_mock_run(store, profile_id, application_id, request_id)
        except AttemptConflict as error:
            raise HTTPException(409, {"code": error.code}) from None

    @app.get("/operations")
    def operations(profile_id: str | None = None):
        from services.operations.progress import operation_public_record
        with store.engine.connect() as connection:
            statement = select(m.operations)
            if profile_id:
                app_ids = select(m.applications.c.id).where(m.applications.c.profile_id == profile_id)
                resume_ids = select(m.uploaded_resumes.c.id).where(m.uploaded_resumes.c.profile_id == profile_id)
                statement = statement.where((m.operations.c.entity_id == profile_id)
                    | m.operations.c.entity_id.in_(app_ids)
                    | m.operations.c.entity_id.in_(resume_ids))
            recent = list(connection.execute(statement.order_by(m.operations.c.updated_at.desc()).limit(100)).mappings())
            # Recovery must remain reachable even after unrelated history grows.
            # Reuse the same profile fence and public serializer for held rows.
            held = connection.execute(statement.where(m.operations.c.kind == 'MANUAL_AUTH',
                (m.operations.c.state == 'RUNNING') | ((m.operations.c.state == 'BLOCKED')
                & (m.operations.c.error == 'BROWSER_RELEASE_UNCONFIRMED'))).order_by(
                    m.operations.c.updated_at.desc())).mappings()
            seen = {row['id'] for row in recent}
            recent.extend(row for row in held if row['id'] not in seen)
            return {"operations": [operation_public_record(row) for row in recent]}

    @app.get("/operations/dead-letter-queue")
    def dead_letter_queue(profile_id: str | None = None):
        from services.operations.progress import operation_public_record
        statement = select(m.operations).where(m.operations.c.state == "DEAD_LETTERED")
        if profile_id:
            app_ids = select(m.applications.c.id).where(m.applications.c.profile_id == profile_id)
            resume_ids = select(m.uploaded_resumes.c.id).where(m.uploaded_resumes.c.profile_id == profile_id)
            statement = statement.where((m.operations.c.entity_id == profile_id)
                | m.operations.c.entity_id.in_(app_ids)
                | m.operations.c.entity_id.in_(resume_ids))
        with store.engine.connect() as connection:
            rows = list(connection.execute(statement.add_columns(func.count().over().label("queue_count")).order_by(
                m.operations.c.dead_lettered_at.desc().nullslast(), m.operations.c.updated_at.desc()).limit(100)).mappings())
            count = int(rows[0]["queue_count"]) if rows else 0
            operations = []
            for row in rows:
                item = operation_public_record(row)
                item.pop("queue_count", None)
                if item["kind"] == "APPLY_MOCK":
                    from services.applications.run_requests import application_retry_blocker
                    blocker = application_retry_blocker(connection, item, lock=False)
                    item["retryable"] = blocker is None
                    if blocker:
                        item["retry_blocked_reason"] = blocker
                else:
                    item["retryable"] = item["kind"] in QUEUES
                    if not item["retryable"]:
                        item["retry_blocked_reason"] = "OPERATION_NOT_RETRYABLE"
                operations.append(item)
            return {"count": count, "operations": operations}

    @app.get("/operations/{operation_id}")
    def operation(operation_id: str):
        from services.operations.progress import operation_public_record
        with store.engine.connect() as connection:
            return operation_public_record(connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one())

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
        from services.operations.progress import operation_public_record
        result = operation_public_record(request_cancel(store, operation_id))
        if result.get("reason") == "RECONCILIATION_REQUIRED":
            return JSONResponse(result, status_code=409)
        return result

    # Keep these routes before the static application fallback. All preparation
    # actions reuse the existing route, fact, and document validation boundary.
    from apps.api.application_rehearsal import create_application_rehearsal_router
    from apps.api.profile_restore import create_profile_restore_router
    from apps.api.pipeline_workbench import create_pipeline_workbench_router
    from apps.api.email_status import create_email_status_router
    app.include_router(create_application_rehearsal_router(store))
    app.include_router(create_profile_restore_router(store, output_dir))
    app.include_router(create_pipeline_workbench_router(
        store, prepare_callback=lambda values, connection: prepare_application(
            ApplicationInput(**values), operation_connection=connection)))
    app.include_router(create_email_status_router(store))

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
