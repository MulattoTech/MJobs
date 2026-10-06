"""Deterministic candidate funnel and per-batch AI work ceilings."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateScore:
    """The minimal deterministic evidence needed for AI-work admission."""

    job_id: str
    deterministic_score: int
    document_reviewed: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.job_id, str) or not self.job_id.strip()
                or self.job_id != self.job_id.strip() or len(self.job_id) > 200):
            raise ValueError("AI_FUNNEL_JOB_ID_INVALID")
        if (type(self.deterministic_score) is not int
                or not 0 <= self.deterministic_score <= 100):
            raise ValueError("AI_FUNNEL_SCORE_INVALID")
        if type(self.document_reviewed) is not bool:
            raise ValueError("AI_FUNNEL_REVIEW_FLAG_INVALID")


@dataclass(frozen=True)
class AIWorkPlan:
    """A bounded plan; constructing it never calls a provider or creates documents."""

    received_count: int
    unique_count: int
    eligible_count: int
    ai_candidate_ids: tuple[str, ...]
    document_candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class AICostPolicy:
    """Configurable deterministic thresholds and work ceilings for one batch."""

    minimum_score: int = 75
    max_input_candidates: int = 5000
    max_ai_candidates: int = 75
    max_document_candidates: int = 20

    def __post_init__(self) -> None:
        limits = (self.minimum_score, self.max_input_candidates,
                  self.max_ai_candidates, self.max_document_candidates)
        if any(type(value) is not int for value in limits):
            raise ValueError("AI_FUNNEL_POLICY_INVALID")
        if (not 0 <= self.minimum_score <= 100
                or not 1 <= self.max_input_candidates <= 100_000
                or not 0 <= self.max_ai_candidates <= 100_000
                or not 0 <= self.max_document_candidates <= self.max_ai_candidates):
            raise ValueError("AI_FUNNEL_POLICY_INVALID")

    def plan(self, candidates: Iterable[CandidateScore]) -> AIWorkPlan:
        """Deduplicate, apply deterministic threshold, then cap AI and reviewed-doc work."""

        if (not isinstance(candidates, Iterable)
                or isinstance(candidates, (str, bytes, Mapping))):
            raise ValueError("AI_FUNNEL_INPUT_INVALID")

        unique: dict[str, CandidateScore] = {}
        received = 0
        for candidate in candidates:
            if received >= self.max_input_candidates:
                raise ValueError("AI_FUNNEL_INPUT_LIMIT")
            received += 1
            if not isinstance(candidate, CandidateScore):
                raise ValueError("AI_FUNNEL_CANDIDATE_INVALID")
            existing = unique.get(candidate.job_id)
            if existing is not None and existing != candidate:
                raise ValueError("AI_FUNNEL_DUPLICATE_CONFLICT")
            unique[candidate.job_id] = candidate

        eligible = [candidate for candidate in unique.values()
                    if candidate.deterministic_score >= self.minimum_score]
        eligible.sort(key=lambda candidate: (
            -candidate.deterministic_score,
            candidate.job_id.casefold(),
            candidate.job_id,
        ))
        ai_candidates = eligible[:self.max_ai_candidates]
        document_candidates = [candidate for candidate in ai_candidates
                               if candidate.document_reviewed]
        return AIWorkPlan(
            received_count=received,
            unique_count=len(unique),
            eligible_count=len(eligible),
            ai_candidate_ids=tuple(candidate.job_id for candidate in ai_candidates),
            document_candidate_ids=tuple(
                candidate.job_id for candidate in document_candidates[:self.max_document_candidates]
            ),
        )


__all__ = ["AICostPolicy", "AIWorkPlan", "CandidateScore"]
