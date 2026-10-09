"""Compare a bounded set of already-scored job snapshots without rescoring.

Input contract for :func:`compare_scored_jobs`:

* ``profile_id``, ``search_profile_id`` and ``job_ids`` are the explicit scope;
  two through ten distinct job IDs must be selected.
* ``snapshots`` contains one mapping per selected job, with ``job_id``, optional
  ``title``/``company``, the existing ``match_score`` and
  ``scoring_contributions``. Optional ``required_requirements`` is a list of
  ``{normalized_term, provenance: {source, evidence_ref?}}`` rows taken from
  stored structured requirement evidence. Terms must already be NFKC-normalized,
  trimmed and case-folded. The service never reads descriptions, skills, or other
  prose to create requirement rows.

The result echoes scope and selected order, copies existing score/contribution
data unchanged, groups a term as shared when it occurs for at least two selected
jobs, and otherwise lists a job-specific term. Missing or invalid score,
contribution, or requirement evidence is represented by scoped ``unknowns``
reason codes; no candidate qualification or new match score is inferred.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from math import isfinite
from typing import Any, Literal, NotRequired, TypedDict
import unicodedata


MIN_SELECTED_JOBS = 2
MAX_SELECTED_JOBS = 10
MAX_SCOPE_ID_LENGTH = 200
MAX_TERM_LENGTH = 160
MAX_SOURCE_LENGTH = 200
MAX_EVIDENCE_REF_LENGTH = 512


class RequirementProvenance(TypedDict):
    source: str
    evidence_ref: NotRequired[str]


class RequiredRequirement(TypedDict):
    normalized_term: str
    provenance: RequirementProvenance


class ComparisonJobSnapshot(TypedDict):
    job_id: str
    title: NotRequired[str | None]
    company: NotRequired[str | None]
    match_score: NotRequired[int | float | None]
    scoring_contributions: NotRequired[list[dict[str, Any]]]
    required_requirements: NotRequired[list[RequiredRequirement]]


class ComparedJob(TypedDict):
    job_id: str
    title: str | None
    company: str | None
    match_score: int | float | None
    scoring_contributions: list[dict[str, Any]]


class SharedRequirementProvenance(RequirementProvenance):
    job_id: str


class SharedRequirement(TypedDict):
    term: str
    job_ids: list[str]
    provenance: list[SharedRequirementProvenance]


class JobSpecificRequirement(TypedDict):
    term: str
    job_id: str
    provenance: RequirementProvenance


ComparisonUnknownCategory = Literal["required_terms", "match_score", "contributions"]
ComparisonUnknownReason = Literal[
    "REQUIREMENTS_MISSING",
    "REQUIREMENTS_INVALID",
    "REQUIREMENT_TERM_UNKNOWN",
    "REQUIREMENT_PROVENANCE_UNKNOWN",
    "MATCH_SCORE_MISSING",
    "MATCH_SCORE_INVALID",
    "SCORING_CONTRIBUTIONS_MISSING",
    "SCORING_CONTRIBUTIONS_INVALID",
]


class ComparisonUnknown(TypedDict):
    category: ComparisonUnknownCategory
    job_ids: list[str]
    reason: ComparisonUnknownReason


class JobComparisonResult(TypedDict):
    profile_id: str
    search_profile_id: str
    job_ids: list[str]
    jobs: list[ComparedJob]
    shared_requirements: list[SharedRequirement]
    job_specific_requirements: list[JobSpecificRequirement]
    unknowns: list[ComparisonUnknown]


class JobComparisonInputError(ValueError):
    """Invalid comparison scope or malformed snapshot envelope."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _validate_scope_id(value: Any) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > MAX_SCOPE_ID_LENGTH):
        raise JobComparisonInputError("SCOPE_ID_INVALID")
    return value


def _add_unknown(
    grouped: dict[tuple[ComparisonUnknownCategory, ComparisonUnknownReason], list[str]],
    category: ComparisonUnknownCategory,
    reason: ComparisonUnknownReason,
    job_id: str,
) -> None:
    job_ids = grouped.setdefault((category, reason), [])
    if job_id not in job_ids:
        job_ids.append(job_id)


def _normalized_term(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_TERM_LENGTH:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized or len(normalized) > MAX_TERM_LENGTH or value != normalized:
        return None
    return normalized


def _provenance(value: Any) -> RequirementProvenance | None:
    if not isinstance(value, Mapping) or set(value) - {"source", "evidence_ref"}:
        return None
    source = value.get("source")
    if (not isinstance(source, str) or not source.strip()
            or len(source) > MAX_SOURCE_LENGTH):
        return None
    result: RequirementProvenance = {"source": source.strip()}
    if "evidence_ref" in value:
        evidence_ref = value["evidence_ref"]
        if (not isinstance(evidence_ref, str) or not evidence_ref.strip()
                or len(evidence_ref) > MAX_EVIDENCE_REF_LENGTH):
            return None
        result["evidence_ref"] = evidence_ref.strip()
    return result


def _score(snapshot: Mapping[str, Any]) -> tuple[int | float | None, str | None]:
    if "match_score" not in snapshot or snapshot["match_score"] is None:
        return None, "MATCH_SCORE_MISSING"
    value = snapshot["match_score"]
    if (type(value) not in (int, float) or value < 0 or value > 100
            or not isfinite(value)):
        return None, "MATCH_SCORE_INVALID"
    return value, None


def _contributions(snapshot: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    if "scoring_contributions" not in snapshot:
        return [], "SCORING_CONTRIBUTIONS_MISSING"
    value = snapshot["scoring_contributions"]
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        return [], "SCORING_CONTRIBUTIONS_INVALID"
    return deepcopy(value), None


def compare_scored_jobs(
    *,
    profile_id: str,
    search_profile_id: str,
    job_ids: Sequence[str],
    snapshots: Sequence[Mapping[str, Any]],
) -> JobComparisonResult:
    """Compare 2–10 explicitly selected snapshots without changing their scores.

    Raises :class:`JobComparisonInputError` for invalid scope, duplicate selected
    IDs, duplicate/malformed snapshot IDs, or a snapshot set that does not match
    the selected IDs. Missing data within an otherwise scoped snapshot is kept as
    UNKNOWN in the result.
    """
    profile_id = _validate_scope_id(profile_id)
    search_profile_id = _validate_scope_id(search_profile_id)
    if isinstance(job_ids, (str, bytes)) or not isinstance(job_ids, Sequence):
        raise JobComparisonInputError("JOB_IDS_INVALID")
    selected_ids = [_validate_scope_id(job_id) for job_id in job_ids]
    if not MIN_SELECTED_JOBS <= len(selected_ids) <= MAX_SELECTED_JOBS:
        raise JobComparisonInputError("JOB_COUNT_OUT_OF_RANGE")
    if len(set(selected_ids)) != len(selected_ids):
        raise JobComparisonInputError("DUPLICATE_JOB_ID")
    if isinstance(snapshots, (str, bytes)) or not isinstance(snapshots, Sequence):
        raise JobComparisonInputError("SNAPSHOTS_INVALID")
    if len(snapshots) != len(selected_ids):
        raise JobComparisonInputError("SNAPSHOT_SCOPE_MISMATCH")

    snapshots_by_id: dict[str, Mapping[str, Any]] = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            raise JobComparisonInputError("SNAPSHOT_INVALID")
        job_id = _validate_scope_id(snapshot.get("job_id"))
        if job_id in snapshots_by_id:
            raise JobComparisonInputError("DUPLICATE_SNAPSHOT_JOB_ID")
        snapshots_by_id[job_id] = snapshot
    if set(snapshots_by_id) != set(selected_ids):
        raise JobComparisonInputError("SNAPSHOT_SCOPE_MISMATCH")

    unknown_groups: dict[
        tuple[ComparisonUnknownCategory, ComparisonUnknownReason], list[str]
    ] = {}
    jobs: list[ComparedJob] = []
    terms_by_job: dict[str, dict[str, dict[tuple[str, str | None], RequirementProvenance]]] = {}

    for job_id in selected_ids:
        snapshot = snapshots_by_id[job_id]
        title = snapshot.get("title")
        company = snapshot.get("company")
        if title is not None and not isinstance(title, str):
            raise JobComparisonInputError("SNAPSHOT_INVALID")
        if company is not None and not isinstance(company, str):
            raise JobComparisonInputError("SNAPSHOT_INVALID")

        match_score, score_unknown = _score(snapshot)
        if score_unknown:
            _add_unknown(unknown_groups, "match_score", score_unknown, job_id)
        contributions, contribution_unknown = _contributions(snapshot)
        if contribution_unknown:
            _add_unknown(unknown_groups, "contributions", contribution_unknown, job_id)
        jobs.append({
            "job_id": job_id,
            "title": title,
            "company": company,
            "match_score": match_score,
            "scoring_contributions": contributions,
        })

        if "required_requirements" not in snapshot:
            _add_unknown(unknown_groups, "required_terms", "REQUIREMENTS_MISSING", job_id)
            continue
        requirements = snapshot["required_requirements"]
        if not isinstance(requirements, list):
            _add_unknown(unknown_groups, "required_terms", "REQUIREMENTS_INVALID", job_id)
            continue

        per_job: dict[str, dict[tuple[str, str | None], RequirementProvenance]] = {}
        for requirement in requirements:
            if not isinstance(requirement, Mapping):
                _add_unknown(
                    unknown_groups, "required_terms", "REQUIREMENT_TERM_UNKNOWN", job_id,
                )
                continue
            term = _normalized_term(requirement.get("normalized_term"))
            provenance = _provenance(requirement.get("provenance"))
            if term is None:
                _add_unknown(
                    unknown_groups, "required_terms", "REQUIREMENT_TERM_UNKNOWN", job_id,
                )
            if provenance is None:
                _add_unknown(
                    unknown_groups, "required_terms", "REQUIREMENT_PROVENANCE_UNKNOWN", job_id,
                )
            if term is None or provenance is None:
                continue
            evidence_ref = provenance.get("evidence_ref")
            provenance_key = (provenance["source"], evidence_ref)
            per_job.setdefault(term, {})[provenance_key] = provenance
        terms_by_job[job_id] = per_job

    term_jobs: dict[str, dict[str, dict[tuple[str, str | None], RequirementProvenance]]] = {}
    for job_id in selected_ids:
        for term, provenance_rows in terms_by_job.get(job_id, {}).items():
            term_jobs.setdefault(term, {})[job_id] = provenance_rows

    shared_requirements: list[SharedRequirement] = []
    job_specific_requirements: list[JobSpecificRequirement] = []
    selected_index = {job_id: index for index, job_id in enumerate(selected_ids)}
    for term in sorted(term_jobs):
        jobs_for_term = term_jobs[term]
        term_job_ids = [job_id for job_id in selected_ids if job_id in jobs_for_term]
        if len(term_job_ids) >= 2:
            provenance_rows: list[SharedRequirementProvenance] = []
            for job_id in term_job_ids:
                for provenance_key in sorted(jobs_for_term[job_id]):
                    provenance = jobs_for_term[job_id][provenance_key]
                    provenance_rows.append({"job_id": job_id, **provenance})
            shared_requirements.append({
                "term": term,
                "job_ids": term_job_ids,
                "provenance": provenance_rows,
            })
            continue
        job_id = term_job_ids[0]
        for provenance_key in sorted(jobs_for_term[job_id]):
            job_specific_requirements.append({
                "term": term,
                "job_id": job_id,
                "provenance": deepcopy(jobs_for_term[job_id][provenance_key]),
            })
    job_specific_requirements.sort(key=lambda row: (
        row["term"], selected_index[row["job_id"]],
        row["provenance"]["source"], row["provenance"].get("evidence_ref", ""),
    ))
    unknowns: list[ComparisonUnknown] = [
        {"category": category, "job_ids": job_ids_for_reason, "reason": reason}
        for (category, reason), job_ids_for_reason in unknown_groups.items()
    ]

    return {
        "profile_id": profile_id,
        "search_profile_id": search_profile_id,
        "job_ids": selected_ids,
        "jobs": jobs,
        "shared_requirements": shared_requirements,
        "job_specific_requirements": job_specific_requirements,
        "unknowns": unknowns,
    }
