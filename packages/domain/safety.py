"""Deterministic truthfulness and submission policy, without side effects."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable


def _boolean(value: object, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _score(value: object, name: str) -> None:
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 100:
        raise ValueError(f"{name} must be finite and between 0 and 100")


def _count(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    source: str
    verified: bool
    user_approved: bool
    confidence: float

    def __post_init__(self) -> None:
        for field in ("id", "text", "source"):
            if not isinstance(getattr(self, field), str):
                raise ValueError(f"{field} must be a string")
        if not self.id.strip():
            raise ValueError("Fact identity must be nonempty")
        _boolean(self.verified, "verified")
        _boolean(self.user_approved, "user_approved")
        _score(self.confidence, "confidence")


@dataclass(frozen=True)
class Policy:
    external_submissions_enabled: bool = False
    minimum_match: float = 80
    minimum_confidence: float = 80
    hourly_limit: int = 3
    daily_limit: int = 10
    company_limit: int = 1

    def __post_init__(self) -> None:
        _boolean(self.external_submissions_enabled, "external_submissions_enabled")
        _score(self.minimum_match, "minimum_match")
        _score(self.minimum_confidence, "minimum_confidence")
        for field in ("hourly_limit", "daily_limit", "company_limit"):
            _count(getattr(self, field), field)


@dataclass(frozen=True)
class SubmissionContext:
    target: str = "EXTERNAL"
    state: str = "PREPARING"
    user_enabled: bool = False
    match_score: float = 0
    confidence: float = 0
    quality_approved: bool = False
    facts_verified: bool = False
    source_allowed: bool = False
    sensitive_answers_approved: bool = False
    duplicate: bool = False
    hourly_count: int = 0
    daily_count: int = 0
    company_count: int = 0
    barrier: str | None = None

    def __post_init__(self) -> None:
        if self.target not in ("MOCK", "EXTERNAL"):
            raise ValueError("Unknown submission target")
        if not isinstance(self.state, str) or not self.state:
            raise ValueError("State must be a nonempty string")
        if self.barrier not in (None, "CAPTCHA", "MFA", "ACCESS_CONTROL", "ANTI_BOT", "RATE_LIMIT"):
            raise ValueError("Unknown intervention barrier")
        for field in ("user_enabled", "quality_approved", "facts_verified", "source_allowed",
                      "sensitive_answers_approved", "duplicate"):
            _boolean(getattr(self, field), field)
        for field in ("match_score", "confidence"):
            _score(getattr(self, field), field)
        for field in ("hourly_count", "daily_count", "company_count"):
            _count(getattr(self, field), field)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_submission(context: SubmissionContext, policy: Policy) -> Decision:
    """All gates apply to mocks too; caller supplies current durable counters."""
    reasons = []
    if context.target == "EXTERNAL":
        if not policy.external_submissions_enabled:
            reasons.append("EXTERNAL_SUBMISSION_DISABLED")
        if not context.user_enabled:
            reasons.append("USER_ENABLEMENT_REQUIRED")
    if context.barrier:
        reasons.append("USER_INTERVENTION_REQUIRED")
    if context.state == "SUBMISSION_UNCONFIRMED":
        reasons.append("SUBMISSION_UNCONFIRMED")
    elif context.state != "READY_TO_SUBMIT":
        reasons.append("INVALID_SUBMISSION_STATE")
    checks = (
        (context.match_score < policy.minimum_match, "MATCH_BELOW_THRESHOLD"),
        (context.confidence < policy.minimum_confidence, "CONFIDENCE_BELOW_THRESHOLD"),
        (not context.quality_approved, "QUALITY_REVIEW_REQUIRED"),
        (not context.facts_verified, "UNVERIFIED_FACTS"),
        (not context.source_allowed, "SOURCE_POLICY_BLOCKED"),
        (not context.sensitive_answers_approved, "SENSITIVE_REVIEW_REQUIRED"),
        (context.duplicate, "DUPLICATE_APPLICATION"),
        (context.hourly_count >= policy.hourly_limit, "HOURLY_LIMIT_REACHED"),
        (context.daily_count >= policy.daily_limit, "DAILY_LIMIT_REACHED"),
        (context.company_count >= policy.company_limit, "COMPANY_LIMIT_REACHED"),
    )
    reasons.extend(reason for failed, reason in checks if failed)
    return Decision(not reasons, tuple(reasons))


def select_verified_facts(profile: Iterable[Fact], requested_ids: Iterable[str]) -> tuple[Fact, ...]:
    """Preserve provenance; missing qualifications cause review, never invention."""
    indexed = {}
    for fact in profile:
        if fact.id in indexed:
            raise ValueError(f"Ambiguous fact identity: {fact.id}")
        indexed[fact.id] = fact
    selected = []
    for fact_id in dict.fromkeys(requested_ids):
        fact = indexed.get(fact_id)
        if (fact is None or not fact.verified or not fact.user_approved
                or not fact.source.strip() or not fact.text.strip() or fact.confidence < 80):
            raise ValueError(f"Missing verified approved fact: {fact_id}")
        selected.append(fact)
    return tuple(selected)
