"""Recalculate deterministic matches against stored jobs without provider requests."""
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, upsert
from services.company_discovery.requests import record_discovery_requests
from services.discovery.skill_taxonomy import canonical_skill_id, extract_skills, skill_label
from services.discovery.search_semantics import normalize_title
from services.audit_history import record_job_discovered, record_match_scored


_STRENGTH_WEIGHT = {"REQUIRED": 1.0, "PREFERRED": 0.5, "NICE_TO_HAVE": 0.25, "UNKNOWN": 0.25}


def _canonical_set(values):
    result = set()
    rows = values if isinstance(values, (list, tuple, set)) else [values] if values else []
    for value in rows:
        identifier = canonical_skill_id(value)
        label = skill_label(identifier) if identifier else None
        result.add((label or str(value)).strip().casefold())
    return result


def _strings(values):
    rows = values if isinstance(values, (list, tuple, set)) else [values] if values else []
    return [str(value).strip() for value in rows if str(value).strip()]


def _title_key(value):
    normalized = normalize_title(value)
    return normalized["title_family"] or normalized["normalized_title"].casefold()


def _fit_factors(job, profile):
    """Describe directly comparable profile preferences and job attributes."""
    attributes = job.get("attributes") or {}
    preferences = profile.get("preferences") or {}
    factors = []

    desired_roles = _strings(preferences.get("desired_roles"))
    title = str(job.get("title") or "").strip()
    if not desired_roles or not title:
        factors.append({
            "id": "title", "label": "Title preference", "status": "UNKNOWN",
            "explanation": "A job title and an explicit desired-role preference are both needed to compare title fit.",
            "score_effect": "NONE",
        })
    else:
        matching_role = next((role for role in desired_roles
                              if _title_key(role) == _title_key(title)), None)
        factors.append({
            "id": "title", "label": "Title preference",
            "status": "ALIGNS" if matching_role else "NO_DIRECT_MATCH",
            "explanation": (f"The job title matches the desired role '{matching_role}' under the local title-family rules."
                            if matching_role else
                            "No direct normalized title match was found for the configured desired roles; this is not a qualification finding."),
            "score_effect": "NONE",
        })

    arrangements = {"ON_SITE" if value == "ONSITE" else value
                    for value in _strings(preferences.get("work_arrangements"))}
    remote_type = str(job.get("remote_type") or "UNKNOWN").upper()
    job_arrangement = ("REMOTE" if remote_type == "UNKNOWN" and attributes.get("remote_indicated") is True else
                       "REMOTE" if remote_type.startswith("REMOTE_") else
                       "ON_SITE" if remote_type == "ONSITE" else remote_type)
    if not arrangements or job_arrangement not in {"REMOTE", "HYBRID", "ON_SITE"}:
        status = "UNKNOWN"
        explanation = "A known job arrangement and an explicit work-arrangement preference are both needed to compare this factor."
    elif job_arrangement in arrangements:
        status = "ALIGNS"
        explanation = f"The known job arrangement ({job_arrangement.replace('_', ' ').lower()}) is in the configured work-arrangement preferences."
    else:
        status = "NO_DIRECT_MATCH"
        explanation = f"The known job arrangement ({job_arrangement.replace('_', ' ').lower()}) is not among the configured work-arrangement preferences; no score penalty is applied."
    factors.append({"id": "work_arrangement", "label": "Work arrangement", "status": status,
                    "explanation": explanation, "score_effect": "NONE"})

    employment_preferences = {value.upper() for value in _strings(preferences.get("employment_types"))}
    employment_type = str(attributes.get("employment_type") or "UNKNOWN").upper()
    if not employment_preferences or employment_type in {"", "UNKNOWN"}:
        status = "UNKNOWN"
        explanation = "A known job employment type and an explicit employment-type preference are both needed to compare this factor."
    elif employment_type in employment_preferences:
        status = "ALIGNS"
        explanation = f"The known job employment type ({employment_type.lower().replace('_', ' ')}) is in the configured preferences."
    else:
        status = "NO_DIRECT_MATCH"
        explanation = f"The known job employment type ({employment_type.lower().replace('_', ' ')}) is not among the configured preferences; no score penalty is applied."
    factors.append({"id": "employment_type", "label": "Employment type", "status": status,
                    "explanation": explanation, "score_effect": "NONE"})
    return factors


def build_explanation(job, profile):
    """Score evidence coverage without turning unclassified mentions into requirements."""
    attributes = job.get("attributes") or {}
    evidence_type = attributes.get("skill_evidence")
    required = _canonical_set(attributes.get("required_skills"))
    preferred = _canonical_set(attributes.get("preferred_skills"))
    nice_to_have = _canonical_set(attributes.get("nice_to_have_skills"))
    all_are_required = evidence_type == "structured_required_skills"
    approved = set()
    for row in profile.get("facts", []):
        if (row["kind"] != "skill" or not row["verified"] or not row["user_approved"]
                or row["confidence"] < 80 or not (row.get("source") or "").strip()):
            continue
        approved.add(row["text"].strip().casefold())
        approved.update(skill.casefold() for skill in extract_skills(row["text"])["labels"])
    skills = []
    seen = set()
    for value in job.get("skills", []):
        identifier = canonical_skill_id(value)
        skill = skill_label(identifier) if identifier else str(value).strip()
        folded = skill.casefold()
        if skill and folded not in seen:
            seen.add(folded)
            skills.append(skill)
    rows = []
    weighted_total = 0.0
    weighted_covered = 0.0
    for skill in skills:
        folded = skill.strip().casefold()
        strength = "REQUIRED" if all_are_required else "UNKNOWN"
        if folded in required:
            strength = "REQUIRED"
        elif folded in preferred:
            strength = "PREFERRED"
        elif folded in nice_to_have:
            strength = "NICE_TO_HAVE"
        weight = _STRENGTH_WEIGHT[strength]
        has_evidence = folded in approved
        weighted_total += weight
        if has_evidence:
            weighted_covered += weight
        rows.append({
            "skill": skill,
            "strength": strength,
            "weight": weight,
            "profile_evidence": "APPROVED" if has_evidence else "NO_APPROVED_EVIDENCE",
            "explanation": ("Supported by an approved career fact." if has_evidence else
                            "No approved career fact supports this skill.") + (
                " The posting does not state whether this mention is required or preferred."
                if strength == "UNKNOWN" else ""),
        })
    matched = sorted(row["skill"] for row in rows if row["profile_evidence"] == "APPROVED")
    missing = sorted(row["skill"] for row in rows if row["profile_evidence"] != "APPROVED")
    score = round(100 * weighted_covered / weighted_total, 2) if weighted_total else 0
    has_classified_skills = bool(required or preferred or nice_to_have)
    structured_full = all_are_required and not preferred and not nice_to_have
    method = ("structured_required_skills" if structured_full else
              "weighted_skill_evidence_coverage" if has_classified_skills or evidence_type not in {None, "description_mentions"} else
              "description_skill_mentions")
    return {
        "score": score,
        "missing": missing,
        "components": {
            "method": method,
            "fit_factors": _fit_factors(job, profile),
            "matched": matched,
            "detected": skills,
            "requirements": rows,
            "limitation": "This score measures evidence coverage, not qualification. Description mentions remain UNKNOWN unless the source classifies them locally and unambiguously; no approved profile evidence does not prove the person lacks a skill.",
        },
    }


def recalculate(store, profile_id, profile_version=None, check=None):
    check = check or (lambda: None)
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == profile_id
            ).with_for_update()).scalar_one()
        profile = store.profile(profile_id, profile_version, connection=connection)
        job_ids = []
        for job in connection.execute(select(m.jobs).order_by(m.jobs.c.id)).mappings():
            check()
            explanation = build_explanation(job, profile)
            had_profile_match = connection.execute(select(m.matches.c.id).where(
                m.matches.c.job_id == job["id"],
                m.matches.c.profile_id == profile_id,
            ).limit(1)).first() is not None
            if not had_profile_match:
                record_job_discovered(connection, profile_id, job["id"])
            record_match_scored(connection, profile_id, job["id"], profile["version"])
            upsert(connection, m.matches, {"id": identity(job["id"], profile_id, profile["version"]), "job_id": job["id"],
                "profile_id": profile_id, "profile_version": profile["version"], **explanation}, replace=True)
            job_ids.append(job['id'])
        record_discovery_requests(connection, job_ids, profile)
        store._audit(connection, profile_id, "MATCHES_RECALCULATED", {"profile_version": profile["version"]})
