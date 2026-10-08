"""Recalculate deterministic matches against stored jobs without provider requests."""
import re

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, upsert
from services.company_discovery.requests import record_discovery_requests
from services.discovery.skill_taxonomy import canonical_skill_id, extract_skills, skill_label
from services.discovery.requirement_parsers import CERTIFICATION_PATTERNS, parse_certifications
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


def _certification_fact_is_explicitly_negative(clause, mention):
    folded = str(clause or "").casefold()
    negative = bool(re.search(
        r"\b(?:do\s+not|don't|does\s+not|doesn't|did\s+not|didn't|have\s+not|haven't|has\s+not|hasn't)"
        r"\s+(?:currently\s+)?(?:hold|have|possess|earned?|obtained?)\b|"
        r"\bno\s+longer\s+(?:currently\s+)?(?:hold|have|possess|earned?|obtained?|valid|active)\b|"
        r"\b(?:formerly|previously)\s+(?:held|had|possessed|certified|earned?|obtained?)\b|"
        r"\bused\s+to\s+(?:hold|have|possess|be\s+certified)\b|"
        r"\b(?:am|is|are|was|were)\s+not\s+(?:currently\s+)?certified\b|"
        r"\bnot\s+a\s+holder\s+of\b|"
        r"\b(?:expired|lapsed|inactive|revoked)\b",
        folded,
    ))
    no_named_certification = bool(re.search(
        r"\bno\s+(?:(?:valid|current|active)\s+)?" + re.escape(str(mention or "").casefold()) + r"\b",
        folded,
    ))
    return negative or no_named_certification


def _certification_identity(identifier, text):
    """Keep locally parsed ITIL versions distinct; other parser ids are canonical."""
    if identifier != "ITIL":
        return identifier, None
    match = re.search(r"\bITIL\s+(v\d+|Foundation)\b", str(text or ""), re.I)
    version = match.group(1).casefold() if match else None
    return identifier, version


def _certification_fit_factors(job, profile):
    """Compare locally parsed job certifications with approved certification facts."""
    attributes = job.get("attributes") or {}
    rows = attributes.get("certifications")
    if not isinstance(rows, list) or not rows:
        return []

    eligible_certification_ids = set()
    for fact in profile.get("facts", []):
        if not isinstance(fact, dict) or fact.get("kind") != "certification":
            continue
        confidence = fact.get("confidence")
        if (fact.get("verified") is not True or fact.get("user_approved") is not True
                or type(confidence) not in (int, float) or confidence < 80
                or not isinstance(fact.get("source"), str) or not fact["source"].strip()
                or not isinstance(fact.get("text"), str) or not fact["text"].strip()):
            continue
        for mention in parse_certifications(fact["text"]):
            if not _certification_fact_is_explicitly_negative(
                    mention.get("clause"), mention.get("raw")):
                eligible_certification_ids.add(
                    _certification_identity(mention["id"], mention.get("raw")))

    names_by_id = {identifier: name for identifier, name, _ in CERTIFICATION_PATTERNS}
    factors_by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get("id")
        if identifier not in names_by_id:
            continue
        identity = _certification_identity(identifier, row.get("raw") or row.get("name"))
        strength = row.get("strength")
        if strength not in {"REQUIRED", "PREFERRED", "NICE_TO_HAVE", "UNKNOWN"}:
            strength = "UNKNOWN"
        name = names_by_id[identifier]
        if identifier == "ITIL" and identity[1]:
            version_label = "Foundation" if identity[1] == "foundation" else identity[1]
            name = f"ITIL {version_label}"
        else:
            name = str(row.get("name") or name).strip() or name
        prior = factors_by_id.get(identity)
        if prior is not None:
            if prior["strength"] != strength:
                prior["strength"] = "UNKNOWN"
            continue
        aligned = identity in eligible_certification_ids
        suffix = f":{identity[1]}" if identity[1] else ""
        factors_by_id[identity] = {
            "id": f"certification:{identifier}{suffix}",
            "name": name,
            "strength": strength,
            "status": "ALIGNS" if aligned else "NO_DIRECT_MATCH",
            "explanation": (
                "An approved career fact contains the same locally parsed certification identity. "
                "This is informational evidence and does not validate the credential with an issuer."
                if aligned else
                "No approved evidence for this certification is present in career facts; "
                "this does not establish that the person lacks it."
            ),
            "score_effect": "NONE",
        }
    return [{
        **factor,
        "label": f"Certification: {factor['name']} ({factor['strength'].replace('_', ' ')})",
    } for factor in factors_by_id.values()]


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
    factors.extend(_certification_fit_factors(job, profile))
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
