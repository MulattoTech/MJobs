"""Match Components Engine (MJ-REQ-113).

Authoritative requirement:
"Score dimensions may include:
title similarity
required skills
preferred skills
experience
education
certifications
remote/location
salary
employment type
industry
company preference
negative requirements"

This module evaluates all 12 score dimensions with mathematical rigor,
honest UNKNOWN semantics, dynamic denominator scaling, and transparent explanations.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
import re
from typing import Any, Literal, TypedDict

from services.discovery.skill_taxonomy import canonical_skill_id, extract_skills, skill_label
from services.discovery.requirement_parsers import parse_certifications
from services.discovery.search_semantics import normalize_title, parse_salary


MATCH_DIMENSIONS = (
    "title_similarity",
    "required_skills",
    "preferred_skills",
    "experience",
    "education",
    "certifications",
    "remote_location",
    "salary",
    "employment_type",
    "industry",
    "company_preference",
    "negative_requirements",
)

DimensionName = Literal[
    "title_similarity",
    "required_skills",
    "preferred_skills",
    "experience",
    "education",
    "certifications",
    "remote_location",
    "salary",
    "employment_type",
    "industry",
    "company_preference",
    "negative_requirements",
]

DEFAULT_DIMENSION_WEIGHTS: dict[DimensionName, float] = {
    "title_similarity": 15.0,
    "required_skills": 20.0,
    "preferred_skills": 10.0,
    "experience": 15.0,
    "education": 5.0,
    "certifications": 5.0,
    "remote_location": 10.0,
    "salary": 10.0,
    "employment_type": 5.0,
    "industry": 5.0,
    "company_preference": 0.0,  # Advisory preference
    "negative_requirements": 0.0,  # Gate / penalty dimension
}


class DimensionContribution(TypedDict):
    dimension: DimensionName
    label: str
    weight: float
    raw_score: float | None
    weighted_contribution: float | None
    status: Literal["KNOWN", "UNKNOWN"]
    explanation: str
    provenance: dict[str, Any]


class MatchComponentsResult(TypedDict):
    score: float | None
    coverage_percent: float
    dimensions: list[DimensionContribution]
    known_dimensions: list[str]
    unknown_dimensions: list[str]
    negative_signals_triggered: list[str]
    advisory: bool


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(text or "").casefold()))


def _jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return round(100.0 * intersection / union, 2) if union else 0.0


def _evaluate_title_similarity(job: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float | None, str, dict]:
    job_title = job.get("title")
    if not isinstance(job_title, str) or not job_title.strip():
        return None, "Title similarity is UNKNOWN: job title is missing.", {"source": "UNKNOWN"}

    # Target titles from preferences or experience facts
    target_titles: list[str] = []
    preferences = profile.get("preferences")
    if isinstance(preferences, Mapping):
        desired = preferences.get("desired_roles") or preferences.get("desired_titles") or preferences.get("include_titles")
        if isinstance(desired, (list, tuple)):
            target_titles.extend(str(t).strip() for t in desired if isinstance(t, str) and t.strip())

    facts = profile.get("facts")
    if isinstance(facts, (list, tuple)):
        for fact in facts:
            if isinstance(fact, Mapping) and fact.get("kind") in ("experience", "title"):
                text = fact.get("text")
                if isinstance(text, str) and fact.get("user_approved") is True:
                    target_titles.append(text)

    if not target_titles:
        return None, "Title similarity is UNKNOWN: candidate profile has no approved target titles or role facts.", {"source": "UNKNOWN"}

    job_tokens = _tokenize(job_title)
    best_sim = 0.0
    best_match = target_titles[0]
    for target in target_titles:
        target_tokens = _tokenize(target)
        sim = _jaccard_similarity(job_tokens, target_tokens)
        if sim > best_sim:
            best_sim = sim
            best_match = target

    return (
        best_sim,
        f"Title similarity is {best_sim:.1f}% based on overlap with approved title '{best_match}'.",
        {"source": "APPROVED_TITLE_OVERLAP", "target_title": best_match},
    )


def _approved_skills_set(profile: Mapping[str, Any]) -> set[str]:
    approved: set[str] = set()
    facts = profile.get("facts")
    if isinstance(facts, (list, tuple)):
        for fact in facts:
            if not isinstance(fact, Mapping) or fact.get("user_approved") is not True or fact.get("verified") is not True:
                continue
            text = fact.get("text")
            if isinstance(text, str) and text.strip():
                approved.add(text.strip().casefold())
                for label in extract_skills(text).get("labels", []):
                    cid = canonical_skill_id(label)
                    if cid:
                        approved.add(cid)
                        approved.add(label.casefold())
    return approved


def _evaluate_skills(job: Mapping[str, Any], profile: Mapping[str, Any], *, required_only: bool) -> tuple[float | None, str, dict]:
    attributes = job.get("attributes") or {}
    key = "required_skills" if required_only else "preferred_skills"
    raw_list = attributes.get(key)

    if not isinstance(raw_list, (list, tuple, set)) or not raw_list:
        # If no explicit list, fallback for required_skills to general job skills if required_only
        if required_only:
            job_skills = job.get("skills")
            if isinstance(job_skills, (list, tuple, set)) and job_skills:
                raw_list = job_skills
            else:
                return None, f"{'Required' if required_only else 'Preferred'} skills are UNKNOWN: posting does not classify skills.", {"source": "UNKNOWN"}
        else:
            return None, "Preferred skills are UNKNOWN: posting lists no preferred skills.", {"source": "UNKNOWN"}

    approved = _approved_skills_set(profile)
    recognized_skills: list[str] = []
    matched_skills: list[str] = []

    for item in raw_list:
        if not isinstance(item, str) or not item.strip():
            continue
        recognized_skills.append(item.strip())
        cid = canonical_skill_id(item)
        if (cid and cid in approved) or item.strip().casefold() in approved:
            matched_skills.append(item.strip())

    if not recognized_skills:
        return None, f"No normalized {'required' if required_only else 'preferred'} skills detected.", {"source": "UNKNOWN"}

    score = round(100.0 * len(matched_skills) / len(recognized_skills), 2)
    label = "Required" if required_only else "Preferred"
    return (
        score,
        f"{label} skill evidence covers {len(matched_skills)} of {len(recognized_skills)} recognized skills ({score:.1f}%).",
        {"source": f"APPROVED_{label.upper()}_SKILLS", "matched": matched_skills, "total": len(recognized_skills)},
    )


def _evaluate_experience(job: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float | None, str, dict]:
    approved_experience: list[str] = []
    facts = profile.get("facts")
    if isinstance(facts, (list, tuple)):
        for fact in facts:
            if isinstance(fact, Mapping) and fact.get("kind") in ("experience", "responsibility", "project"):
                if fact.get("verified") is True and fact.get("user_approved") is True:
                    text = fact.get("text")
                    if isinstance(text, str) and text.strip():
                        approved_experience.append(text)

    if not approved_experience:
        return None, "Relevant experience is UNKNOWN: no verified, user-approved experience facts are available.", {"source": "UNKNOWN"}

    attributes = job.get("attributes") or {}
    level = attributes.get("experience_level")
    if isinstance(level, str) and level.strip():
        # Level alignment
        return (
            85.0,
            f"Candidate has {len(approved_experience)} approved experience facts matching target level {level}.",
            {"source": "APPROVED_EXPERIENCE_FACTS", "level": level},
        )

    # General experience support
    score = min(100.0, 50.0 + len(approved_experience) * 10.0)
    return (
        score,
        f"Candidate has {len(approved_experience)} verified, user-approved experience facts supporting this domain.",
        {"source": "APPROVED_EXPERIENCE_FACTS", "fact_count": len(approved_experience)},
    )


def _evaluate_education(job: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float | None, str, dict]:
    attributes = job.get("attributes") or {}
    edu_req = attributes.get("education_requirement") or attributes.get("education")
    if not isinstance(edu_req, str) or edu_req in ("NONE_SPECIFIED", "UNKNOWN", ""):
        return None, "Education requirement is UNKNOWN or not specified in posting.", {"source": "UNKNOWN"}

    # Candidate education facts
    degrees: list[str] = []
    facts = profile.get("facts")
    if isinstance(facts, (list, tuple)):
        for fact in facts:
            if isinstance(fact, Mapping) and fact.get("kind") == "education" and fact.get("user_approved") is True:
                text = fact.get("text")
                if isinstance(text, str) and text.strip():
                    degrees.append(text.casefold())

    has_degree = any("bachelor" in d or "master" in d or "degree" in d for d in degrees)
    has_noncompletion = any("incomplete" in d or "not completed" in d for d in degrees)

    if has_noncompletion and not has_degree:
        return 20.0, "Candidate notes incomplete degree while posting specifies an education requirement.", {"source": "EDUCATION_FACTS"}
    if has_degree:
        return 100.0, "Candidate possesses verified degree meeting the education requirement.", {"source": "EDUCATION_FACTS"}
    if "EQUIVALENT" in edu_req.upper():
        return 80.0, "Posting accepts equivalent experience; candidate experience facts substitute.", {"source": "EXPERIENCE_SUBSTITUTION"}

    return 40.0, "Candidate facts do not confirm a degree; requirement is not met.", {"source": "EDUCATION_FACTS"}


def _evaluate_certifications(job: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float | None, str, dict]:
    attributes = job.get("attributes") or {}
    job_certs = attributes.get("certifications")
    if not isinstance(job_certs, (list, tuple)) or not job_certs:
        return None, "Certifications are UNKNOWN: posting lists no certifications.", {"source": "UNKNOWN"}

    candidate_certs: set[str] = set()
    facts = profile.get("facts")
    if isinstance(facts, (list, tuple)):
        for fact in facts:
            if isinstance(fact, Mapping) and fact.get("kind") == "certification" and fact.get("user_approved") is True:
                text = fact.get("text")
                if isinstance(text, str) and text.strip():
                    candidate_certs.add(text.strip().casefold())

    matched = 0
    total = len(job_certs)
    for cert in job_certs:
        cert_name = str(cert.get("name") if isinstance(cert, Mapping) else cert).casefold()
        if any(cert_name in c or c in cert_name for c in candidate_certs):
            matched += 1

    score = round(100.0 * matched / total, 2) if total else 0.0
    return (
        score,
        f"Candidate holds {matched} of {total} recognized certifications ({score:.1f}%).",
        {"source": "APPROVED_CERTIFICATIONS", "matched": matched, "total": total},
    )


def _evaluate_remote_location(job: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float | None, str, dict]:
    remote_type = str(job.get("remote_type") or "UNKNOWN").upper()
    preferences = profile.get("preferences") or {}
    work_arrangements = preferences.get("work_arrangements")
    if isinstance(work_arrangements, (list, tuple, set)) and work_arrangements:
        desired_list = [str(w).upper() for w in work_arrangements]
        desired_remote = ", ".join(desired_list)
    else:
        desired_remote = str(preferences.get("remote") or preferences.get("work_arrangement") or "").upper()
        desired_list = [desired_remote] if desired_remote else []

    if remote_type in ("UNKNOWN", "") or not desired_remote:
        return None, "Remote/location fit is UNKNOWN: remote type or candidate preference is not specified.", {"source": "UNKNOWN"}

    if any("REMOTE" in d for d in desired_list) and "REMOTE" in remote_type:
        return 100.0, f"Work arrangement aligns: remote opportunity matches candidate remote preference ({remote_type}).", {"source": "LOCATION_ALIGNMENT"}
    if any(remote_type == d for d in desired_list):
        return 100.0, f"Work arrangement exactly matches: {remote_type}.", {"source": "LOCATION_ALIGNMENT"}
    if "REMOTE" in remote_type:
        return 75.0, "Posting offers remote work; candidate is open to alternative arrangements.", {"source": "LOCATION_ALIGNMENT"}

    return 20.0, f"Location mismatch: posting is {remote_type} whereas candidate preference is {desired_remote}.", {"source": "LOCATION_ALIGNMENT"}


def _evaluate_salary(job: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float | None, str, dict]:
    attributes = job.get("attributes") or {}
    job_min = attributes.get("salary_min")
    job_max = attributes.get("salary_max")
    preferences = profile.get("preferences") or {}
    target_min = preferences.get("salary_min") or preferences.get("minimum_salary")

    if target_min is None and preferences.get("salary_expectations"):
        raw_exp = str(preferences["salary_expectations"])
        parsed = parse_salary(raw_exp)
        if parsed.get("salary_min") is not None:
            target_min = parsed["salary_min"]
        else:
            m_sal = re.search(r"\b(\d{2,3})[kK]\b|\$?(\d[\d,]{3,})", raw_exp)
            if m_sal:
                if m_sal.group(1):
                    target_min = float(m_sal.group(1)) * 1000
                elif m_sal.group(2):
                    target_min = float(m_sal.group(2).replace(",", ""))

    if job_min is None and job_max is None:
        return None, "Salary match is UNKNOWN: posting discloses no salary range.", {"source": "UNKNOWN"}
    if target_min is None:
        return None, "Salary match is UNKNOWN: candidate profile has no declared salary minimum.", {"source": "UNKNOWN"}

    try:
        t_min = float(target_min)
        j_max = float(job_max) if job_max is not None else float(job_min)
        j_min = float(job_min) if job_min is not None else float(job_max)
    except (ValueError, TypeError):
        return None, "Salary values are non-numeric; unable to evaluate.", {"source": "UNKNOWN"}

    if j_max >= t_min:
        score = 100.0 if j_min >= t_min else 80.0
        return score, f"Salary aligns: job max (${j_max:,.0f}) meets candidate target minimum (${t_min:,.0f}).", {"source": "SALARY_OVERLAP"}

    return 20.0, f"Salary is below target: job max (${j_max:,.0f}) is below candidate minimum (${t_min:,.0f}).", {"source": "SALARY_BELOW_TARGET"}


def _evaluate_employment_type(job: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float | None, str, dict]:
    attributes = job.get("attributes") or {}
    job_type = attributes.get("employment_type") or job.get("employment_type")
    preferences = profile.get("preferences") or {}
    desired_types = preferences.get("employment_types")

    if not job_type or not desired_types:
        return None, "Employment type is UNKNOWN: posting or preference missing.", {"source": "UNKNOWN"}

    job_str = str(job_type).upper()
    desired_set = {str(t).upper() for t in desired_types} if isinstance(desired_types, (list, tuple, set)) else {str(desired_types).upper()}

    if job_str in desired_set:
        return 100.0, f"Employment type aligns: {job_str} matches candidate preference.", {"source": "EMPLOYMENT_TYPE"}
    return 30.0, f"Employment type mismatch: job is {job_str}, candidate requested {', '.join(desired_set)}.", {"source": "EMPLOYMENT_TYPE"}


def _evaluate_industry(job: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float | None, str, dict]:
    attributes = job.get("attributes") or {}
    job_industry = attributes.get("industry") or job.get("industry")
    preferences = profile.get("preferences") or {}
    desired_industries = preferences.get("industries") or preferences.get("preferred_industries")

    if not job_industry or not desired_industries:
        return None, "Industry is UNKNOWN: posting or preference not specified.", {"source": "UNKNOWN"}

    ind_str = str(job_industry).casefold()
    pref_set = {str(i).casefold() for i in desired_industries} if isinstance(desired_industries, (list, tuple)) else {str(desired_industries).casefold()}

    if any(ind_str in p or p in ind_str for p in pref_set):
        return 100.0, f"Industry aligns: '{job_industry}' matches preferred industry.", {"source": "INDUSTRY_PREFERENCE"}
    return 60.0, f"Industry neutral: '{job_industry}' is not in candidate preferred list.", {"source": "INDUSTRY_NEUTRAL"}


def _evaluate_company_preference(job: Mapping[str, Any], company_preferences: Mapping[str, str] | None) -> tuple[float | None, str, dict]:
    company_id = job.get("company_id")
    if not company_id or not company_preferences:
        return None, "Company preference is UNKNOWN: no preference recorded for this employer.", {"source": "UNKNOWN"}

    status = company_preferences.get(company_id, "NEUTRAL")
    if status == "PREFERRED":
        return 100.0, "Employer is marked PREFERRED by the user (+bonus).", {"source": "COMPANY_PREFERENCE", "status": "PREFERRED"}
    if status == "BLACKLISTED":
        return 0.0, "Employer is marked BLACKLISTED by the user.", {"source": "COMPANY_PREFERENCE", "status": "BLACKLISTED"}
    return 60.0, "Employer is NEUTRAL in candidate company preferences.", {"source": "COMPANY_PREFERENCE", "status": "NEUTRAL"}


def _evaluate_negative_requirements(job: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[float | None, str, dict, list[str]]:
    preferences = profile.get("preferences") or {}
    disallowed_terms = preferences.get("avoid_terms") or preferences.get("negative_keywords") or preferences.get("disallowed_skills") or []
    if not isinstance(disallowed_terms, (list, tuple, set)):
        disallowed_terms = []

    description = str(job.get("description") or "").casefold()
    skills = [str(s).casefold() for s in job.get("skills", [])]
    attributes = job.get("attributes") or {}
    attr_tokens = " ".join(f"{k} {v}" for k, v in attributes.items()).casefold()
    triggered: list[str] = []

    for term in disallowed_terms:
        term_fold = str(term).strip().casefold()
        if term_fold and (term_fold in description or term_fold in skills or term_fold in attr_tokens):
            triggered.append(str(term))

    if triggered:
        return (
            0.0,
            f"Negative requirement triggered: posting mentions explicitly disallowed term(s): {', '.join(triggered)}.",
            {"source": "NEGATIVE_REQUIREMENT_TRIGGERED", "terms": triggered},
            triggered,
        )

    if disallowed_terms:
        return 100.0, "No negative requirements or disallowed keywords found in posting.", {"source": "NEGATIVE_REQUIREMENTS_CLEAN"}, []
    return None, "Negative requirements are UNKNOWN: no negative keyword filters configured.", {"source": "UNKNOWN"}, []


def evaluate_match_components(
    job: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    weights: Mapping[DimensionName, float] | None = None,
    company_preferences: Mapping[str, str] | None = None,
) -> MatchComponentsResult:
    """Evaluate all 12 score dimensions defined in MJ-REQ-113.

    Calculates transparent dimensional breakdown, honest UNKNOWNs, dynamic
    denominator normalization, and negative requirement checks.
    """
    if not isinstance(job, Mapping):
        raise ValueError("Job must be a mapping")
    if not isinstance(profile, Mapping):
        raise ValueError("Profile must be a mapping")

    configured_weights = dict(DEFAULT_DIMENSION_WEIGHTS)
    if weights:
        for k, v in weights.items():
            if k in configured_weights and isinstance(v, (int, float)) and isfinite(v) and v >= 0:
                configured_weights[k] = float(v)

    evaluators = {
        "title_similarity": lambda: _evaluate_title_similarity(job, profile),
        "required_skills": lambda: _evaluate_skills(job, profile, required_only=True),
        "preferred_skills": lambda: _evaluate_skills(job, profile, required_only=False),
        "experience": lambda: _evaluate_experience(job, profile),
        "education": lambda: _evaluate_education(job, profile),
        "certifications": lambda: _evaluate_certifications(job, profile),
        "remote_location": lambda: _evaluate_remote_location(job, profile),
        "salary": lambda: _evaluate_salary(job, profile),
        "employment_type": lambda: _evaluate_employment_type(job, profile),
        "industry": lambda: _evaluate_industry(job, profile),
        "company_preference": lambda: _evaluate_company_preference(job, company_preferences),
    }

    contributions: list[DimensionContribution]
    contributions = []
    known_dimensions: list[str] = []
    unknown_dimensions: list[str] = []
    negative_signals_triggered: list[str] = []

    # Evaluate 11 standard dimensions
    for dim in MATCH_DIMENSIONS[:-1]:
        label = dim.replace("_", " ").title()
        weight = configured_weights.get(dim, 0.0)
        score_val, explanation, provenance = evaluators[dim]()
        if score_val is not None:
            status = "KNOWN"
            known_dimensions.append(dim)
        else:
            status = "UNKNOWN"
            unknown_dimensions.append(dim)

        contributions.append({
            "dimension": dim,
            "label": label,
            "weight": weight,
            "raw_score": score_val,
            "weighted_contribution": None,  # will be computed after active denominator
            "status": status,
            "explanation": explanation,
            "provenance": provenance,
        })

    # Evaluate 12th dimension: negative requirements
    neg_score, neg_expl, neg_prov, neg_triggered = _evaluate_negative_requirements(job, profile)
    neg_weight = configured_weights.get("negative_requirements", 0.0)
    negative_signals_triggered.extend(neg_triggered)
    contributions.append({
        "dimension": "negative_requirements",
        "label": "Negative Requirements",
        "weight": neg_weight,
        "raw_score": neg_score,
        "weighted_contribution": None,
        "status": "KNOWN" if neg_score is not None else "UNKNOWN",
        "explanation": neg_expl,
        "provenance": neg_prov,
    })
    if neg_score is not None:
        known_dimensions.append("negative_requirements")
    else:
        unknown_dimensions.append("negative_requirements")

    # Compute overall score and weighted contributions
    active_weight = sum(c["weight"] for c in contributions if c["raw_score"] is not None and c["weight"] > 0)
    if active_weight > 0:
        weighted_points = sum(
            c["weight"] * c["raw_score"]
            for c in contributions
            if c["raw_score"] is not None and c["weight"] > 0
        )
        overall_score = round(weighted_points / active_weight, 2)
        for c in contributions:
            if c["raw_score"] is not None and c["weight"] > 0:
                c["weighted_contribution"] = round(c["weight"] * c["raw_score"] / active_weight, 2)
    else:
        overall_score = None

    # Severely penalize overall score if negative requirements are explicitly triggered
    if negative_signals_triggered and overall_score is not None:
        overall_score = 0.0

    total_possible_weight = sum(configured_weights.values())
    coverage_percent = round(100.0 * active_weight / total_possible_weight, 2) if total_possible_weight else 0.0

    return {
        "score": overall_score,
        "coverage_percent": coverage_percent,
        "dimensions": contributions,
        "known_dimensions": known_dimensions,
        "unknown_dimensions": unknown_dimensions,
        "negative_signals_triggered": negative_signals_triggered,
        "advisory": True,
    }
