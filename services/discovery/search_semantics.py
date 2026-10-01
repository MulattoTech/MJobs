"""Deterministic job/search semantics for filtering and normalized facets."""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
import re

REMOTE_TYPES = {
    "REMOTE_WORLDWIDE",
    "REMOTE_US",
    "REMOTE_COUNTRY",
    "REMOTE_SPECIFIC_STATES",
    "HYBRID",
    "ONSITE",
    "UNKNOWN",
}
EMPLOYMENT_TYPES = {
    "FULL_TIME",
    "PART_TIME",
    "CONTRACT",
    "CONTRACT_TO_HIRE",
    "TEMPORARY",
    "INTERNSHIP",
    "OTHER",
    "UNKNOWN",
}
US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}

def _fold(value):
    return str(value or "").casefold()

def normalize_employment_type(text):
    value = _fold(text)
    checks = (
        ("CONTRACT_TO_HIRE", (r"contract[- ]to[- ]hire", r"contract to hire")),
        ("FULL_TIME", (r"\bfull[- ]time\b", r"\bfte\b")),
        ("PART_TIME", (r"\bpart[- ]time\b",)),
        ("INTERNSHIP", (r"\bintern(ship)?\b",)),
        ("TEMPORARY", (r"\btemporary\b", r"\btemp\b")),
        ("CONTRACT", (r"\bcontract(or)?\b", r"\b1099\b")),
    )
    for result, patterns in checks:
        if any(re.search(pattern, value) for pattern in patterns):
            return result
    return "UNKNOWN"

def _state_codes(text):
    return sorted(set(re.findall(r"(?<![A-Z])(" + "|".join(sorted(US_STATES)) + r")(?![A-Z])", str(text or "").upper())))

def classify_location(location_text, description=""):
    location = str(location_text or "").strip() or "Unknown"
    combined = f"{location} {description or ''}"
    folded = combined.casefold()
    remote_indicated = bool(re.search(r"\bremote\b", folded))
    hybrid_indicated = bool(re.search(r"\bhybrid\b", folded))
    onsite_indicated = bool(re.search(r"\b(on[- ]?site|onsite)\b", folded))
    state_codes = _state_codes(combined)

    country = None
    if re.search(r"\b(united states|u.?s.?a?|us)\b", folded):
        country = "US"
    elif "canada" in folded:
        country = "CA"
    elif "united kingdom" in folded or re.search(r"\buk\b", folded):
        country = "GB"

    parts = [part.strip() for part in location.split(",") if part.strip()]
    city = None
    state = None
    if parts:
        first = parts[0]
        if not re.search(r"\b(remote|hybrid|onsite|on-site)\b", first, re.I):
            city = first[:200]
    if state_codes:
        state = state_codes[0]

    restricted_states = []
    restriction = re.search(
        r"(?:must|may|eligible|applicants?)[^.;]{0,80}(?:reside|live|located)[^.;]{0,40}(?:in|within)\s*:?\s*([A-Z]{2}(?:\s*,\s*[A-Z]{2})+)",
        combined,
        re.I,
    )
    if restriction:
        restricted_states = _state_codes(restriction.group(1))
    elif remote_indicated and len(state_codes) >= 2:
        restricted_states = state_codes

    remote_regions = []
    if restricted_states:
        remote_type = "REMOTE_SPECIFIC_STATES"
        remote_regions = restricted_states
    elif hybrid_indicated:
        remote_type = "HYBRID"
    elif onsite_indicated and not remote_indicated:
        remote_type = "ONSITE"
    elif remote_indicated and re.search(r"\b(worldwide|anywhere|global)\b", folded):
        remote_type = "REMOTE_WORLDWIDE"
        remote_regions = ["WORLDWIDE"]
    elif remote_indicated and country == "US":
        remote_type = "REMOTE_US"
        remote_regions = ["US"]
    elif remote_indicated and country:
        remote_type = "REMOTE_COUNTRY"
        remote_regions = [country]
    elif remote_indicated:
        remote_type = "UNKNOWN"
    else:
        remote_type = "UNKNOWN"

    return {
        "remote_type": remote_type,
        "remote_regions": remote_regions,
        "remote_indicated": remote_indicated,
        "country": country,
        "state": state,
        "city": city,
        "location_text": location,
    }

def _aliases(criteria):
    raw = criteria.get("technology_aliases") or {}
    result = {}
    for canonical, values in raw.items():
        result[_fold(canonical)] = {_fold(canonical), *(_fold(value) for value in values)}
    return result

def _contains(term, corpus, aliases):
    folded = _fold(term)
    candidates = aliases.get(folded, {folded})
    return any(candidate and candidate in corpus for candidate in candidates)

def _haversine_miles(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 3958.7613 * 2 * asin(sqrt(value))

def evaluate_search(criteria, job):
    attrs = dict(job.get("attributes") or {})
    title = _fold(job.get("title"))
    company = _fold(job.get("company"))
    description = _fold(job.get("description"))
    skills = " ".join(_fold(value) for value in (job.get("skills") or []))
    location = _fold(job.get("location"))
    corpus = " ".join((title, company, description, skills, location))
    aliases = _aliases(criteria)
    reasons = []

    include_titles = [_fold(v) for v in criteria.get("include_titles", [])]
    if include_titles and not any(value in title for value in include_titles):
        reasons.append("TITLE_NOT_INCLUDED")
    if any(_fold(value) in title for value in criteria.get("exclude_titles", [])):
        reasons.append("TITLE_EXCLUDED")

    for value in criteria.get("required_keywords", []):
        if not _contains(value, corpus, aliases):
            reasons.append("REQUIRED_KEYWORD_MISSING:" + str(value))
    for value in criteria.get("excluded_keywords", []):
        if _contains(value, corpus, aliases):
            reasons.append("EXCLUDED_KEYWORD:" + str(value))
    for value in criteria.get("exact_phrases", []):
        if _fold(value) not in corpus:
            reasons.append("EXACT_PHRASE_MISSING:" + str(value))

    company_include = [_fold(v) for v in criteria.get("company_include", [])]
    if company_include and not any(value in company for value in company_include):
        reasons.append("COMPANY_NOT_INCLUDED")
    if any(_fold(value) in company for value in criteria.get("company_exclude", [])):
        reasons.append("COMPANY_EXCLUDED")

    remote_type = str(job.get("remote_type") or "UNKNOWN")
    arrangements = set(criteria.get("work_arrangements") or [])
    arrangement = "REMOTE" if remote_type.startswith("REMOTE_") or remote_type == "REMOTE" or attrs.get("remote_indicated") else remote_type
    remote_preference = criteria.get("remote", "ANY")
    if remote_preference == "REQUIRED" and arrangement != "REMOTE":
        reasons.append("REMOTE_REQUIRED")
    elif remote_preference == "EXCLUDED" and arrangement == "REMOTE":
        reasons.append("REMOTE_EXCLUDED")
    if arrangements and arrangement not in arrangements:
        reasons.append("WORK_ARRANGEMENT_MISMATCH")

    for key, job_key in (("countries", "country"), ("states", "state"), ("cities", "city")):
        allowed = {_fold(v) for v in criteria.get(key, [])}
        if allowed and _fold(attrs.get(job_key)) not in allowed:
            reasons.append(key.upper() + "_MISMATCH")

    allowed_regions = {_fold(v) for v in criteria.get("remote_regions", [])}
    job_regions = {_fold(v) for v in attrs.get("remote_regions", [])}
    if allowed_regions and not (allowed_regions & job_regions):
        reasons.append("REMOTE_REGION_MISMATCH")

    employment = set(criteria.get("employment_types") or [])
    if employment and attrs.get("employment_type", "UNKNOWN") not in employment:
        reasons.append("EMPLOYMENT_TYPE_MISMATCH")

    radius = criteria.get("radius_miles")
    if radius is not None:
        coordinates = attrs.get("coordinates") or {}
        required = (criteria.get("radius_latitude"), criteria.get("radius_longitude"),
                    coordinates.get("latitude"), coordinates.get("longitude"))
        if any(value is None for value in required):
            reasons.append("RADIUS_DATA_UNAVAILABLE")
        elif _haversine_miles(*required) > radius:
            reasons.append("OUTSIDE_RADIUS")

    optional = [value for value in criteria.get("optional_keywords", []) if _contains(value, corpus, aliases)]
    return {"matched": not reasons, "reasons": reasons, "optional_matches": optional}

def filter_jobs(rows, criteria):
    results = []
    for row in rows:
        result = evaluate_search(criteria, row)
        if result["matched"]:
            item = dict(row)
            item["search_evidence"] = result
            results.append(item)
    return results
