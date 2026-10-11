"""Requirement Strength & Differential Scoring Engine (MJ-REQ-115).

Authoritative specification:
"Requirement extraction must distinguish:
REQUIRED
PREFERRED
NICE_TO_HAVE
UNKNOWN

Missing a preferred technology should not penalize the score as severely as missing a mandatory qualification."

This module implements deterministic qualification strength extraction,
differential penalty bounds, and structured alignment analysis.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
import re
from typing import Any, Literal, TypedDict

from services.discovery.skill_taxonomy import canonical_skill_id, extract_skills, skill_label
from services.discovery.requirement_parsers import parse_certifications, parse_experience_requirements


RequirementStrength = Literal["REQUIRED", "PREFERRED", "NICE_TO_HAVE", "UNKNOWN"]
QualificationType = Literal["SKILL", "CERTIFICATION", "EDUCATION", "EXPERIENCE"]
CandidateCoverage = Literal["COVERED", "MISSING", "UNKNOWN"]

STRENGTH_WEIGHTS: dict[RequirementStrength, float] = {
    "REQUIRED": 1.0,
    "PREFERRED": 0.5,
    "NICE_TO_HAVE": 0.25,
    "UNKNOWN": 0.25,
}


class QualificationItem(TypedDict):
    name: str
    qualification_type: QualificationType
    strength: RequirementStrength
    weight: float
    candidate_status: CandidateCoverage
    score_impact: str
    provenance: dict[str, Any]


class QualificationSummary(TypedDict):
    total: int
    covered: int
    missing: int
    unknown: int
    coverage_ratio: float


class QualificationStrengthAnalysis(TypedDict):
    items: list[QualificationItem]
    by_strength: dict[RequirementStrength, list[QualificationItem]]
    counts: dict[RequirementStrength, QualificationSummary]
    required_coverage_percent: float
    preferred_coverage_percent: float
    overall_strength_score: float
    penalty_differential_valid: bool
    explanation: str


def _clean_str(val: Any) -> str:
    return str(val or "").strip()


def classify_strength_from_clause(text: str) -> RequirementStrength:
    """Classify the requirement strength from sentence/clause phrasing."""
    folded = text.casefold()

    # Negations of mandatory
    if re.search(r"\b(?:not|never|no longer)\s+(?:strictly\s+)?(?:required|mandatory)\b", folded):
        return "PREFERRED" if re.search(r"\b(?:preferred|desired|plus)\b", folded) else "UNKNOWN"

    if re.search(r"\b(required|must have|mandatory|essential|minimum qualification)\b", folded):
        return "REQUIRED"
    if re.search(r"\b(preferred|desired|strongly preferred|prioritized)\b", folded):
        return "PREFERRED"
    if re.search(r"\b(nice to have|plus|bonus|ideal|optional)\b", folded):
        return "NICE_TO_HAVE"

    return "UNKNOWN"


def _candidate_verified_skills(profile: Mapping[str, Any]) -> set[str]:
    approved: set[str] = set()
    facts = profile.get("facts")
    if isinstance(facts, (list, tuple)):
        for fact in facts:
            if isinstance(fact, Mapping) and fact.get("user_approved") is True and fact.get("verified") is True:
                text = fact.get("text")
                if isinstance(text, str) and text.strip():
                    approved.add(text.strip().casefold())
                    cid = canonical_skill_id(text)
                    if cid:
                        approved.add(cid)
                    for label in extract_skills(text).get("labels", []):
                        approved.add(label.casefold())
                        c_label = canonical_skill_id(label)
                        if c_label:
                            approved.add(c_label)
    return approved


def extract_qualification_items(
    job: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> list[QualificationItem]:
    """Extract and categorize all qualification items into the 4 distinct strengths."""
    items: list[QualificationItem] = []
    seen_names: set[str] = set()

    attributes = job.get("attributes") or {}
    verified_skills = _candidate_verified_skills(profile)

    # 1. Structured skills from attributes
    required_raw = attributes.get("required_skills") or []
    preferred_raw = attributes.get("preferred_skills") or []
    nice_raw = attributes.get("nice_to_have_skills") or []

    for name in required_raw:
        s_name = _clean_str(name)
        if s_name and s_name.casefold() not in seen_names:
            seen_names.add(s_name.casefold())
            cid = canonical_skill_id(s_name)
            is_covered = (cid and cid in verified_skills) or s_name.casefold() in verified_skills
            items.append({
                "name": s_name,
                "qualification_type": "SKILL",
                "strength": "REQUIRED",
                "weight": STRENGTH_WEIGHTS["REQUIRED"],
                "candidate_status": "COVERED" if is_covered else "MISSING",
                "score_impact": (
                    "Mandatory qualification satisfied (+1.0 weight)."
                    if is_covered
                    else "Missing mandatory qualification — core requirement not met (-1.0 weight impact)."
                ),
                "provenance": {"source": "ATTRIBUTES_REQUIRED_SKILLS", "canonical_id": cid},
            })

    for name in preferred_raw:
        s_name = _clean_str(name)
        if s_name and s_name.casefold() not in seen_names:
            seen_names.add(s_name.casefold())
            cid = canonical_skill_id(s_name)
            is_covered = (cid and cid in verified_skills) or s_name.casefold() in verified_skills
            items.append({
                "name": s_name,
                "qualification_type": "SKILL",
                "strength": "PREFERRED",
                "weight": STRENGTH_WEIGHTS["PREFERRED"],
                "candidate_status": "COVERED" if is_covered else "MISSING",
                "score_impact": (
                    "Preferred qualification satisfied (+0.5 bonus weight)."
                    if is_covered
                    else "Missing preferred qualification — desirable but not disqualifying (capped at -0.5 weight impact)."
                ),
                "provenance": {"source": "ATTRIBUTES_PREFERRED_SKILLS", "canonical_id": cid},
            })

    for name in nice_raw:
        s_name = _clean_str(name)
        if s_name and s_name.casefold() not in seen_names:
            seen_names.add(s_name.casefold())
            cid = canonical_skill_id(s_name)
            is_covered = (cid and cid in verified_skills) or s_name.casefold() in verified_skills
            items.append({
                "name": s_name,
                "qualification_type": "SKILL",
                "strength": "NICE_TO_HAVE",
                "weight": STRENGTH_WEIGHTS["NICE_TO_HAVE"],
                "candidate_status": "COVERED" if is_covered else "MISSING",
                "score_impact": (
                    "Nice-to-have qualification satisfied (+0.25 bonus weight)."
                    if is_covered
                    else "Bonus qualification not met; does not disqualify or heavily penalize candidate."
                ),
                "provenance": {"source": "ATTRIBUTES_NICE_TO_HAVE_SKILLS", "canonical_id": cid},
            })

    # 2. General job skills if not already categorized
    job_skills = job.get("skills") or []
    description = _clean_str(job.get("description"))

    for raw_skill in job_skills:
        s_name = _clean_str(raw_skill)
        if s_name and s_name.casefold() not in seen_names:
            seen_names.add(s_name.casefold())
            # Find context in description to classify strength
            strength: RequirementStrength = "UNKNOWN"
            if description:
                # Find sentence mentioning this skill
                match = re.search(rf"[^.?!;\n]*\b{re.escape(s_name)}\b[^.?!;\n]*", description, re.I)
                if match:
                    strength = classify_strength_from_clause(match.group(0))

            cid = canonical_skill_id(s_name)
            is_covered = (cid and cid in verified_skills) or s_name.casefold() in verified_skills
            weight = STRENGTH_WEIGHTS[strength]
            items.append({
                "name": s_name,
                "qualification_type": "SKILL",
                "strength": strength,
                "weight": weight,
                "candidate_status": "COVERED" if is_covered else "MISSING",
                "score_impact": (
                    f"{strength.replace('_', ' ').capitalize()} skill covered (+{weight:.2f} weight)."
                    if is_covered
                    else f"Uncovered {strength.replace('_', ' ').lower()} skill ({weight:.2f} weight impact)."
                ),
                "provenance": {"source": "JOB_SKILLS_DESCRIPTION_MENTION", "canonical_id": cid},
            })

    # 3. Certifications
    certs = parse_certifications(description)
    candidate_certs = {
        _clean_str(f.get("text")).casefold()
        for f in profile.get("facts", [])
        if isinstance(f, Mapping) and f.get("kind") == "certification" and f.get("user_approved") is True and f.get("verified") is True
    }
    for cert in certs:
        c_name = cert["name"]
        if c_name.casefold() not in seen_names:
            seen_names.add(c_name.casefold())
            strength = cert.get("strength", "UNKNOWN")
            is_covered = any(c_name.casefold() in cc or cc in c_name.casefold() for cc in candidate_certs)
            weight = STRENGTH_WEIGHTS.get(strength, 0.25)
            items.append({
                "name": c_name,
                "qualification_type": "CERTIFICATION",
                "strength": strength,
                "weight": weight,
                "candidate_status": "COVERED" if is_covered else "MISSING",
                "score_impact": (
                    f"Certification satisfied (+{weight:.2f} weight)."
                    if is_covered
                    else f"Missing {strength.lower()} certification ({weight:.2f} weight impact)."
                ),
                "provenance": {"source": "PARSED_CERTIFICATION", "raw_clause": cert.get("clause")},
            })

    return items


def analyze_qualification_strengths(
    job: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> QualificationStrengthAnalysis:
    """Analyze all qualifications distinguishing REQUIRED, PREFERRED, NICE_TO_HAVE, and UNKNOWN."""
    items = extract_qualification_items(job, profile)

    by_strength: dict[RequirementStrength, list[QualificationItem]] = {
        "REQUIRED": [],
        "PREFERRED": [],
        "NICE_TO_HAVE": [],
        "UNKNOWN": [],
    }

    for item in items:
        by_strength[item["strength"]].append(item)

    counts: dict[RequirementStrength, QualificationSummary] = {}
    for strength, group in by_strength.items():
        total = len(group)
        covered = sum(1 for i in group if i["candidate_status"] == "COVERED")
        missing = sum(1 for i in group if i["candidate_status"] == "MISSING")
        unknown = sum(1 for i in group if i["candidate_status"] == "UNKNOWN")
        ratio = (covered / total) if total > 0 else 1.0
        counts[strength] = {
            "total": total,
            "covered": covered,
            "missing": missing,
            "unknown": unknown,
            "coverage_ratio": round(ratio, 4),
        }

    # Bounded differential penalty verification
    # Required item missing penalty = 1.0 weight
    # Preferred item missing penalty = 0.5 weight (50% of required)
    # Nice to have = 0.25 weight (25% of required)
    penalty_differential_valid = (
        STRENGTH_WEIGHTS["PREFERRED"] < STRENGTH_WEIGHTS["REQUIRED"]
        and STRENGTH_WEIGHTS["NICE_TO_HAVE"] < STRENGTH_WEIGHTS["PREFERRED"]
    )

    req_summary = counts["REQUIRED"]
    pref_summary = counts["PREFERRED"]
    req_pct = round(req_summary["coverage_ratio"] * 100.0, 1) if req_summary["total"] > 0 else 100.0
    pref_pct = round(pref_summary["coverage_ratio"] * 100.0, 1) if pref_summary["total"] > 0 else 100.0

    # Calculate overall weighted strength score
    total_weight = sum(item["weight"] for item in items)
    covered_weight = sum(item["weight"] for item in items if item["candidate_status"] == "COVERED")
    overall_score = round(100.0 * covered_weight / total_weight, 1) if total_weight > 0 else 100.0

    req_msg = f"{req_summary['covered']}/{req_summary['total']} required met ({req_pct}%)"
    pref_msg = f"{pref_summary['covered']}/{pref_summary['total']} preferred met ({pref_pct}%)"
    explanation = f"Qualification strength analysis: {req_msg}; {pref_msg}."

    return {
        "items": items,
        "by_strength": by_strength,
        "counts": counts,
        "required_coverage_percent": req_pct,
        "preferred_coverage_percent": pref_pct,
        "overall_strength_score": overall_score,
        "penalty_differential_valid": penalty_differential_valid,
        "explanation": explanation,
    }
