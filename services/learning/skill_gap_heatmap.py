"""Deterministic skill evidence summaries over persisted current-version matches."""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity
from services.discovery.skill_taxonomy import canonical_skill_id, skill_label


_CLASSIFIED_STRENGTHS = {"REQUIRED", "PREFERRED", "NICE_TO_HAVE"}
_INTERVIEW_STATUSES = {"INTERVIEW", "TECHNICAL_INTERVIEW", "FINAL_INTERVIEW"}
_USER_ATTRIBUTIONS = {"USER_REPORTED", "EXTERNAL_USER_REPORTED"}


def _current_matches(store, profile_id: str, profile_version: str) -> list[dict]:
    with store.engine.connect() as connection:
        return [dict(row) for row in connection.execute(
            select(m.matches.c.job_id, m.matches.c.missing, m.matches.c.components)
            .where(m.matches.c.profile_id == profile_id,
                   m.matches.c.profile_version == profile_version)
            .order_by(m.matches.c.job_id)
        ).mappings()]


def _reported_events(store, profile_id: str, job_ids: set[str]) -> dict[str, dict[str, set[str]]]:
    """Read only explicit user-reported status and external-application events."""
    result: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"applied": set(), "interview": set()}
    )
    if not job_ids:
        return result
    with store.engine.connect() as connection:
        applications = list(connection.execute(
            select(m.applications.c.id, m.applications.c.job_id)
            .where(m.applications.c.profile_id == profile_id,
                   m.applications.c.job_id.in_(job_ids))
        ).mappings())
        entity_to_job = {
            identity("application-tracking", profile_id, row["id"]): row["job_id"]
            for row in applications
        }
        if not entity_to_job:
            return result
        events = connection.execute(
            select(m.audit.c.entity_id, m.audit.c.action, m.audit.c.payload)
            .where(m.audit.c.entity_id.in_(entity_to_job),
                   m.audit.c.action.in_(
                       ("APPLICATION_PIPELINE_STATUS", "EXTERNAL_APPLICATION_REPORTED")
                   ))
        ).mappings()
        for event in events:
            job_id = entity_to_job[event["entity_id"]]
            if event["action"] == "EXTERNAL_APPLICATION_REPORTED":
                result[job_id]["applied"].add(job_id)
                continue
            payload = event["payload"] or {}
            if payload.get("attribution") not in _USER_ATTRIBUTIONS:
                continue
            status = payload.get("status")
            if status == "APPLIED":
                result[job_id]["applied"].add(job_id)
            if status in _INTERVIEW_STATUSES:
                result[job_id]["interview"].add(job_id)
    return result


def _aggregate(matches: list[dict], reported: dict[str, dict[str, set[str]]]) -> dict:
    # Track job ids per signal so duplicated requirement rows cannot inflate a count.
    totals: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {
            "jobs": set(), "required": set(), "preferred": set(),
            "nice_to_have": set(), "unknown_metadata": set(),
            "no_approved_evidence": set(), "approved_evidence": set(),
            "applied": set(), "interview": set(),
            "gap_required": set(), "gap_applied": set(), "gap_interview": set(),
        }
    )
    for match in matches:
        job_id = match["job_id"]
        components = match.get("components") or {}
        requirements = components.get("requirements")
        if not isinstance(requirements, list):
            requirements = []
            # Older or incomplete match records have no strength/evidence detail.
            # Retain their missing labels as uncertain posting metadata only.
            for value in match.get("missing") or []:
                requirements.append({"skill": value, "strength": "UNKNOWN"})

        per_job: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: {"strengths": set(), "evidence": set()}
        )
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            skill_id = canonical_skill_id(requirement.get("skill"))
            if not skill_id:
                continue
            strength = str(requirement.get("strength") or "UNKNOWN").upper()
            evidence = str(requirement.get("profile_evidence") or "UNKNOWN").upper()
            per_job[skill_id]["strengths"].add(
                strength if strength in _CLASSIFIED_STRENGTHS or strength == "UNKNOWN"
                else "UNKNOWN"
            )
            per_job[skill_id]["evidence"].add(evidence)

        for skill_id, signals in per_job.items():
            row = totals[skill_id]
            row["jobs"].add(job_id)
            strengths = signals["strengths"]
            evidence = signals["evidence"]
            if "REQUIRED" in strengths:
                row["required"].add(job_id)
            if "PREFERRED" in strengths:
                row["preferred"].add(job_id)
            if "NICE_TO_HAVE" in strengths:
                row["nice_to_have"].add(job_id)
            if "UNKNOWN" in strengths:
                row["unknown_metadata"].add(job_id)
            if "APPROVED" in evidence:
                row["approved_evidence"].add(job_id)
            has_evidence_gap = (
                "NO_APPROVED_EVIDENCE" in evidence
                and bool(strengths & _CLASSIFIED_STRENGTHS)
            )
            if has_evidence_gap:
                row["no_approved_evidence"].add(job_id)
                if "REQUIRED" in strengths:
                    row["gap_required"].add(job_id)
            user_events = reported.get(job_id) or {}
            if user_events.get("applied"):
                row["applied"].add(job_id)
                if has_evidence_gap:
                    row["gap_applied"].add(job_id)
            if user_events.get("interview"):
                row["interview"].add(job_id)
                if has_evidence_gap:
                    row["gap_interview"].add(job_id)

    skills = []
    for skill_id, signals in totals.items():
        skills.append({
            "skill_id": skill_id,
            "skill": skill_label(skill_id) or skill_id,
            "job_count": len(signals["jobs"]),
            "required_job_count": len(signals["required"]),
            "preferred_job_count": len(signals["preferred"]),
            "nice_to_have_job_count": len(signals["nice_to_have"]),
            "unknown_posting_metadata_job_count": len(signals["unknown_metadata"]),
            "no_approved_evidence_job_count": len(signals["no_approved_evidence"]),
            "approved_evidence_job_count": len(signals["approved_evidence"]),
            "applied_job_count": len(signals["applied"]),
            "interview_job_count": len(signals["interview"]),
            "no_evidence_required_job_count": len(signals["gap_required"]),
            "no_evidence_applied_job_count": len(signals["gap_applied"]),
            "no_evidence_interview_job_count": len(signals["gap_interview"]),
        })
    skills.sort(key=lambda row: (
        -row["no_evidence_interview_job_count"],
        -row["no_evidence_applied_job_count"],
        -row["no_evidence_required_job_count"],
        -row["no_approved_evidence_job_count"],
        row["skill_id"],
    ))
    return {"jobs_examined": len(matches), "skills": skills}


def get_skill_gap_heatmap(store, profile_id: str,
                          profile_version: str | None = None) -> dict:
    """Summarize current match evidence and explicit user-reported milestones.

    A match with no approved profile evidence does not establish that a person
    lacks a skill. Unclassified posting mentions remain separate metadata.
    Counts are raw distinct-job frequencies; no composite score is inferred.
    """
    profile = store.profile(profile_id)
    current_version = profile["version"]
    if profile_version is not None and profile_version != current_version:
        raise ValueError("Requested profile version is stale")
    matches = _current_matches(store, profile_id, current_version)
    job_ids = {row["job_id"] for row in matches}
    reported = _reported_events(store, profile_id, job_ids)
    result = _aggregate(matches, reported)
    return {"profile_version": current_version, **result}
