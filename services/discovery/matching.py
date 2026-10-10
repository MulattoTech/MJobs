"""Recalculate deterministic matches against stored jobs without provider requests."""
import re
from math import isfinite

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, upsert
from services.company_discovery.requests import record_discovery_requests
from services.discovery.skill_taxonomy import canonical_skill_id, extract_skills, skill_label
from services.discovery.requirement_parsers import CERTIFICATION_PATTERNS, parse_certifications
from services.discovery.search_semantics import normalize_title, parse_salary
from services.audit_history import record_job_discovered, record_match_scored
from services.matching.skill_evidence import collect_positive_skill_evidence, EXPERIENCE_KINDS


_STRENGTH_WEIGHT = {"REQUIRED": 1.0, "PREFERRED": 0.5, "NICE_TO_HAVE": 0.25, "UNKNOWN": 0.25}
_CURRENCY_CODES = re.compile(r"\b(?:USD|CAD|AUD|NZD|EUR|GBP|JPY|CNY|INR|CHF|MXN|BRL|SEK|NOK|DKK|ZAR)\b", re.I)
_SALARY_RANGE = re.compile(
    r"\$?\s*\d[\d,]*(?:\.\d+)?\s*(?P<low_scale>[kK])?\s*"
    r"(?:-|–|—|to)\s*\$?\s*\d[\d,]*(?:\.\d+)?\s*(?P<high_scale>[kK])?\b",
    re.I,
)
_BACHELOR_MENTION = re.compile(r"\bbachelor(?:'s|s)?(?:\s+degree)?\b|\bbachelor\s+of\s+[a-z][a-z -]{0,40}\b", re.I)
_BACHELOR_NEGATION = re.compile(
    r"\b(?:do\s+not|don't|does\s+not|doesn't|did\s+not|didn't|have\s+not|haven't|has\s+not|hasn't|never)\s+"
    r"(?:currently\s+)?(?:have|hold|earned?|obtain(?:ed)?|complete(?:d)?|receive(?:d)?|possess)?\s*"
    r"(?:a\s+)?(?:bachelor(?:'s|s)?(?:\s+degree)?|bachelor\s+of\s+[a-z][a-z -]{0,40})\b|"
    r"\bno\s+(?:bachelor(?:'s|s)?(?:\s+degree)?|bachelor\s+of\s+[a-z][a-z -]{0,40})\b|"
    r"\b(?:lack|lacking)\s+(?:a\s+)?(?:bachelor(?:'s|s)?(?:\s+degree)?|bachelor\s+of\s+[a-z][a-z -]{0,40})\b",
    re.I,
)
_BACHELOR_NONCOMPLETION = re.compile(
    r"\b(?:bachelor(?:'s|s)?(?:\s+degree)?|bachelor\s+of\s+[a-z][a-z -]{0,40})"
    r"[^.;\n]{0,80}\b(?:(?:not\s+(?:yet\s+)?|never\s+)(?:completed|complete|earned|finished|obtained)|incomplete|unfinished)\b|"
    r"\b(?:incomplete|unfinished)\s+(?:a\s+)?(?:bachelor(?:'s|s)?(?:\s+degree)?|bachelor\s+of\s+[a-z][a-z -]{0,40})\b",
    re.I,
)
_EDUCATION_AMBIGUITY = re.compile(
    r"\b(?:in\s+progress|working\s+toward|pursuing|candidate\s+for|expected\s+(?:to\s+)?(?:graduate|complete|earn)|"
    r"not\s+yet\s+(?:earned|completed)|or\s+equivalent\s+experience)\b",
    re.I,
)
_US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_STATE_CODES_BY_NAME = {name.casefold(): code for code, name in _US_STATE_NAMES.items()}


def _country_code(value):
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    aliases = {
        "us": "US", "usa": "US", "united states": "US",
        "united states of america": "US", "ca": "CA", "canada": "CA",
        "gb": "GB", "uk": "GB", "united kingdom": "GB",
        "great britain": "GB",
    }
    return aliases.get(normalized)


def _state_code(value):
    normalized = re.sub(r"[^a-z]+", " ", str(value or "").casefold()).strip()
    if len(normalized) == 2 and normalized.upper() in _US_STATE_NAMES:
        return normalized.upper()
    return _STATE_CODES_BY_NAME.get(normalized)


def _remote_location_fit_factor(job, profile):
    attributes = job.get("attributes") or {}
    remote_type = str(job.get("remote_type") or "UNKNOWN").upper()
    raw_regions = attributes.get("remote_regions")
    regions = [str(value).strip() for value in raw_regions
               if isinstance(value, str) and value.strip()] if isinstance(raw_regions, (list, tuple, set)) else []
    contact = profile.get("contact") or {}
    country = _country_code(contact.get("country")) if isinstance(contact, dict) else None
    state = _state_code(contact.get("state")) if isinstance(contact, dict) else None

    normalized_regions = set()
    unknown_region = False
    for region in regions:
        token = re.sub(r"[^a-z0-9]+", " ", region.casefold()).strip()
        if token in {"worldwide", "global", "anywhere"}:
            normalized_regions.add("WORLDWIDE")
        elif code := _country_code(region):
            normalized_regions.add(code)
        elif code := _state_code(region):
            normalized_regions.add(f"US:{code}")
        else:
            unknown_region = True

    state_regions = {region.split(":", 1)[1] for region in normalized_regions
                     if region.startswith("US:")}
    country_regions = normalized_regions - {"WORLDWIDE"} - {
        f"US:{code}" for code in state_regions
    }
    if (not remote_type.startswith("REMOTE") or not regions or unknown_region
            or country is None):
        status = "UNKNOWN"
        explanation = (
            "A supported remote-work scope and the selected profile's country are both needed; "
            "on-site commute distance and work authorization are not inferred."
        )
    elif "WORLDWIDE" in normalized_regions:
        status = "ALIGNS"
        explanation = (
            "The known remote-work regions are worldwide; "
            "this does not verify work authorization or change the score."
        )
    elif remote_type == "REMOTE_SPECIFIC_STATES" or (country == "US" and state_regions):
        if not state_regions:
            status = "UNKNOWN"
            explanation = (
                "The job is marked as limited to specific U.S. states, but those states are "
                "not available as supported regions."
            )
        elif country != "US":
            status = "NO_DIRECT_MATCH"
            explanation = (
                "The job is limited to U.S. states and the selected profile country is outside "
                "the United States; this informational result does not reduce the score."
            )
        elif state is None:
            status = "UNKNOWN"
            explanation = (
                "The job is limited to specific U.S. states, but a supported profile state "
                "is unavailable; no location fit is inferred."
            )
        elif state in state_regions:
            status = "ALIGNS"
            explanation = (
                "The selected profile state is within the job's stated remote-work regions; "
                "this does not verify work authorization or change the score."
            )
        else:
            status = "NO_DIRECT_MATCH"
            explanation = (
                "The selected profile state is outside the job's stated remote-work regions; "
                "this informational result does not reduce the score."
            )
    elif country in country_regions:
        status = "ALIGNS"
        explanation = (
            "The known remote-work regions include the selected profile's country; "
            "this does not verify work authorization or change the score."
        )
    else:
        status = "NO_DIRECT_MATCH"
        explanation = (
            "The known remote-work regions do not include the selected profile's country; "
            "this informational result does not reduce the score."
        )
    return {
        "id": "remote_location", "label": "Remote work region",
        "status": status, "explanation": explanation, "score_effect": "NONE",
    }


def _has_mixed_salary_range_scale(text):
    match = _SALARY_RANGE.search(text)
    return bool(match and bool(match.group("low_scale")) != bool(match.group("high_scale")))


def _annual_usd_interval(value, *, job=False):
    """Return finite closed annual USD bounds only when their currency is explicit."""
    if job:
        if (value.get("salary_currency") != "USD" or value.get("salary_period") != "YEAR"
                or value.get("salary_source") not in {"EMPLOYER_PROVIDED", "SOURCE_PROVIDED"}):
            return None
        low, high = value.get("salary_min"), value.get("salary_max")
        if (type(low) not in (int, float) or type(high) not in (int, float)
                or not isfinite(low) or not isfinite(high) or low < 0 or low > high):
            return None
        return float(low), float(high)

    text = value if isinstance(value, str) else ""
    if not text.strip():
        return None
    currencies = {match.group(0).upper() for match in _CURRENCY_CODES.finditer(text)}
    has_dollar = "$" in text
    if currencies - {"USD"} or len(currencies) > 1 or not ("USD" in currencies or has_dollar):
        return None
    parse_text = _CURRENCY_CODES.sub(lambda match: "$" if match.group(0).upper() == "USD" else match.group(0), text)
    # The shared parser scales only the endpoint carrying "k" (for example
    # 90-110k becomes 90-110000). The intended shared scale is ambiguous, so
    # leave mixed-suffix ranges unknown rather than infer the user's meaning.
    if _has_mixed_salary_range_scale(parse_text):
        return None
    parsed = parse_salary(parse_text)
    low, high = parsed["salary_min"], parsed["salary_max"]
    if (parsed["salary_period"] != "YEAR" or type(low) not in (int, float)
            or type(high) not in (int, float) or not isfinite(low) or not isfinite(high)
            or low > high):
        return None
    return float(low), float(high)


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
        r"\bno\s+longer\s+(?:currently\s+)?(?:hold|have|possess|earned?|obtained?|valid|active|current|certified|credentialed|licensed)\b|"
        r"\bnot\s+(?:(?:currently|presently)\s+)?(?:certified|current|active|valid|credentialed|licensed)\b|"
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


def _education_fit_factors(job, profile):
    """Compare only explicitly classified bachelor requirements and approved education facts."""
    classification = (job.get("attributes") or {}).get("education_requirement")
    supported = {
        "BACHELORS_REQUIRED": ("REQUIRED", "bachelors"),
        "BACHELORS_PREFERRED": ("PREFERRED", "bachelors"),
        "BACHELORS_OR_EQUIVALENT_EXPERIENCE": ("UNKNOWN", "bachelors"),
        "DEGREE_PREFERRED": ("PREFERRED", "degree"),
    }
    requirement = supported.get(classification)
    if classification in (None, "", "NONE_SPECIFIED"):
        return []
    if requirement is None:
        return [{
            "id": "education:unsupported",
            "label": "Education requirement (UNKNOWN)",
            "status": "UNKNOWN",
            "explanation": "The persisted education classification is unsupported, so no education fit claim is made.",
            "score_effect": "NONE",
        }]

    strength, identity = requirement
    positive = False
    negative = False
    ambiguous = False
    for fact in profile.get("facts", []) if identity == "bachelors" else []:
        if not isinstance(fact, dict) or fact.get("kind") != "education":
            continue
        confidence = fact.get("confidence")
        text = fact.get("text")
        source = fact.get("source")
        if (fact.get("verified") is not True or fact.get("user_approved") is not True
                or type(confidence) not in (int, float) or confidence < 80
                or not isinstance(text, str) or not text.strip()
                or not isinstance(source, str) or not source.strip()):
            continue
        if not _BACHELOR_MENTION.search(text):
            continue
        if _EDUCATION_AMBIGUITY.search(text):
            ambiguous = True
        elif _BACHELOR_NONCOMPLETION.search(text) or _BACHELOR_NEGATION.search(text):
            negative = True
        else:
            positive = True

    if strength == "UNKNOWN":
        status = "UNKNOWN"
        explanation = (
            "The persisted education classification includes equivalent experience, so this factor "
            "does not determine whether degree evidence alone fits."
        )
    elif positive and negative:
        status = "UNKNOWN"
        explanation = "Approved education facts conflict about bachelor's degree evidence."
    elif ambiguous:
        status = "UNKNOWN"
        explanation = "Approved education evidence mentions a bachelor's degree but does not clearly establish completion."
    elif positive:
        status = "ALIGNS"
        explanation = "An approved, sufficiently confident education fact explicitly records a bachelor's degree."
    elif negative:
        status = "NO_DIRECT_MATCH"
        explanation = "An approved, sufficiently confident education fact explicitly states that a bachelor's degree was not completed; this is not inferred from missing records."
    elif identity == "degree":
        status = "UNKNOWN"
        explanation = "The job states a preferred degree without identifying a degree type that can be compared safely."
    else:
        status = "UNKNOWN"
        explanation = "No approved, sufficiently confident bachelor's degree evidence is available; this does not establish that the person lacks a degree."

    name = "Bachelor's degree" if identity == "bachelors" else "Degree"
    return [{
        "id": f"education:{identity}",
        "label": f"Education: {name} ({strength.replace('_', ' ')})",
        "status": status,
        "explanation": explanation,
        "score_effect": "NONE",
    }]


def _relevant_experience_fit_factor(job, profile):
    """Compare job skills with approved experience facts without inferring qualifications."""
    target_labels = {}
    for value in _strings(job.get("skills")):
        identifier = canonical_skill_id(value)
        if not identifier:
            continue
        label = skill_label(identifier)
        if label:
            target_labels[label.casefold()] = label
    target_skills = set(target_labels)
    if not target_skills:
        return {
            "id": "relevant_experience", "label": "Relevant experience", "status": "UNKNOWN",
            "explanation": "The posting has no supported skill evidence to compare with career experience.",
            "score_effect": "NONE",
        }

    experience_evidence = collect_positive_skill_evidence(profile.get("facts"), EXPERIENCE_KINDS)
    experience_skills = _canonical_set(experience_evidence["labels"])

    if not experience_skills:
        status = "UNKNOWN"
        explanation = (
            "No approved, sufficiently confident experience facts contain positive locally recognized skill evidence; "
            "missing profile evidence does not show that relevant experience is absent."
        )
    else:
        matched = sorted(target_labels[key] for key in target_skills & experience_skills)
        if matched:
            status = "ALIGNS"
            explanation = (
                "Approved experience facts positively support locally recognized job skills: "
                + ", ".join(matched) + "."
            )
        else:
            status = "NO_DIRECT_MATCH"
            explanation = (
                "No direct overlap was found between locally recognized job skills and approved experience facts; "
                "other relevant experience may not be represented in those facts."
            )
    return {
        "id": "relevant_experience", "label": "Relevant experience", "status": status,
        "explanation": explanation, "score_effect": "NONE",
    }


def build_preference_fit_factors(job, profile):
    """Build pure, score-neutral preference evidence shared by both scorers.

    These comparisons describe preferences, not candidate qualifications. Callers
    may explicitly weight them, but must never use source-provided fit scores.
    """
    attributes = job.get("attributes") or {}
    preferences = profile.get("preferences") or {}
    factors = []

    expected_salary = _annual_usd_interval(preferences.get("salary_expectations"))
    posted_salary = _annual_usd_interval(attributes, job=True)
    if expected_salary is None or posted_salary is None:
        salary_status = "UNKNOWN"
        salary_explanation = (
            "A directly stated annual USD preference and a complete annual USD posting range "
            "are both needed to compare salary fit."
        )
    elif max(expected_salary[0], posted_salary[0]) <= min(expected_salary[1], posted_salary[1]):
        salary_status = "ALIGNS"
        salary_explanation = "The stated annual USD preference and posting ranges overlap."
    else:
        salary_status = "NO_DIRECT_MATCH"
        salary_explanation = "The stated annual USD preference and posting ranges do not overlap."
    factors.append({"id": "salary", "label": "Salary expectation", "status": salary_status,
                    "explanation": salary_explanation, "score_effect": "NONE"})

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
    factors.append(_remote_location_fit_factor(job, profile))

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


def _fit_factors(job, profile):
    factors = build_preference_fit_factors(job, profile)
    factors.extend(_certification_fit_factors(job, profile))
    factors.extend(_education_fit_factors(job, profile))
    factors.append(_relevant_experience_fit_factor(job, profile))
    return factors


def build_explanation(job, profile):
    """Score evidence coverage without turning unclassified mentions into requirements."""
    attributes = job.get("attributes") or {}
    evidence_type = attributes.get("skill_evidence")
    required = _canonical_set(attributes.get("required_skills"))
    preferred = _canonical_set(attributes.get("preferred_skills"))
    nice_to_have = _canonical_set(attributes.get("nice_to_have_skills"))
    all_are_required = evidence_type == "structured_required_skills"
    career_evidence = collect_positive_skill_evidence(profile.get("facts"), {"skill"})
    approved = set(career_evidence["exact"])
    approved.update(label.casefold() for label in career_evidence["labels"])
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
        identifier = canonical_skill_id(skill)
        if has_evidence:
            evidence_reason = "APPROVED_POSITIVE_EVIDENCE"
            evidence_explanation = "Supported by an approved career fact containing positive skill evidence."
        elif identifier in career_evidence["conflicting_ids"]:
            evidence_reason = "CONFLICTING_APPROVED_EVIDENCE"
            evidence_explanation = "Approved career facts conflict about this skill; review them before awarding evidence credit."
        elif identifier in career_evidence["withheld_ids"]:
            evidence_reason = "EXPLICIT_NEGATIVE_OR_UNCERTAIN_EVIDENCE"
            evidence_explanation = "Explicitly negative or aspirational career wording is not positive skill evidence."
        else:
            evidence_reason = "NO_APPROVED_EVIDENCE"
            evidence_explanation = "No approved career fact positively supports this skill."
        weighted_total += weight
        if has_evidence:
            weighted_covered += weight
        rows.append({
            "skill": skill,
            "strength": strength,
            "weight": weight,
            "covered_weight": weight if has_evidence else 0.0,
            "profile_evidence": "APPROVED" if has_evidence else "NO_APPROVED_EVIDENCE",
            "evidence_reason": evidence_reason,
            "explanation": evidence_explanation + (
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
            "skill_evidence_coverage": {
                "covered_weight": round(weighted_covered, 2),
                "possible_weight": round(weighted_total, 2),
                "percentage": score,
            },
            "fit_factors": _fit_factors(job, profile),
            "matched": matched,
            "detected": skills,
            "requirements": rows,
            "limitation": "This score measures evidence coverage, not qualification. Description mentions remain UNKNOWN unless the source classifies them locally and unambiguously; no approved profile evidence does not prove the person lacks a skill.",
        },
    }


MAX_CANDIDATE_JOB_IDS = 10_000
MAX_JOB_ID_LENGTH = 128
JOB_ID_QUERY_CHUNK_SIZE = 500


def _normalize_candidate_job_ids(candidate_job_ids):
    if candidate_job_ids is None:
        return None
    if not isinstance(candidate_job_ids, (list, tuple)) or len(candidate_job_ids) > MAX_CANDIDATE_JOB_IDS:
        raise ValueError("Candidate job IDs must be a bounded list of IDs")
    if any(not isinstance(job_id, str) or not job_id or len(job_id) > MAX_JOB_ID_LENGTH
           for job_id in candidate_job_ids):
        raise ValueError("Candidate job IDs must contain non-empty IDs up to 128 characters")
    return sorted(set(candidate_job_ids))


def _candidate_jobs(connection, candidate_job_ids):
    if candidate_job_ids is None:
        return list(connection.execute(select(m.jobs).order_by(m.jobs.c.id)).mappings())
    jobs = []
    for offset in range(0, len(candidate_job_ids), JOB_ID_QUERY_CHUNK_SIZE):
        chunk = candidate_job_ids[offset:offset + JOB_ID_QUERY_CHUNK_SIZE]
        jobs.extend(connection.execute(select(m.jobs).where(
            m.jobs.c.id.in_(chunk))).mappings())
    return sorted(jobs, key=lambda job: job["id"])


def recalculate(store, profile_id, profile_version=None, check=None, *, candidate_job_ids=None,
                scoring_criteria=None):
    check = check or (lambda: None)
    candidate_job_ids = _normalize_candidate_job_ids(candidate_job_ids)
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == profile_id
            ).with_for_update()).scalar_one()
        profile = store.profile(profile_id, profile_version, connection=connection)
        job_ids = []
        for job in _candidate_jobs(connection, candidate_job_ids):
            check()
            explanation = build_explanation(job, profile)
            if scoring_criteria is not None:
                from services.matching.selected_search_scoring import compose_selected_search_match
                composed = compose_selected_search_match(
                    job, {"score": explanation["score"]}, scoring_criteria,
                    candidate_profile=profile,
                )
                explanation["score"] = composed["score"]
                explanation["components"] = {
                    **explanation["components"],
                    "scoring_profile": composed["scoring_profile"],
                    "scoring_contributions": composed["scoring_contributions"],
                }
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
