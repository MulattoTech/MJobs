"""Deterministic job/search semantics for filtering and normalized facets."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
    "FULL_TIME", "PART_TIME", "CONTRACT", "CONTRACT_TO_HIRE",
    "TEMPORARY", "INTERNSHIP", "OTHER", "UNKNOWN",
}
EXPERIENCE_LEVELS = {
    "INTERNSHIP", "ENTRY", "JUNIOR", "ASSOCIATE", "MID", "SENIOR",
    "LEAD", "MANAGER", "DIRECTOR", "EXECUTIVE", "UNKNOWN",
}
SALARY_PERIODS = {"HOUR", "YEAR", "UNKNOWN"}
SALARY_SOURCES = {"EMPLOYER_PROVIDED", "SOURCE_PROVIDED", "ESTIMATED", "UNKNOWN"}
DATE_WINDOWS = {"ANY", "LAST_HOUR", "TODAY", "LAST_24_HOURS", "LAST_3_DAYS", "LAST_7_DAYS", "LAST_30_DAYS", "CUSTOM"}
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

def normalize_experience_level(text):
    value = str(text or "")
    folded = value.casefold()
    patterns = (
        ("EXECUTIVE", r"\b(chief|c-suite|vice president|vp)\b"),
        ("DIRECTOR", r"\bdirector\b"),
        ("MANAGER", r"\bmanager\b"),
        ("LEAD", r"\b(lead|principal|staff)\b"),
        ("SENIOR", r"\b(senior|sr\.?)(?:\s|$)"),
        ("MID", r"\b(mid[- ]level|midlevel|intermediate)\b"),
        ("ASSOCIATE", r"\bassociate\b"),
        ("JUNIOR", r"\b(junior|jr\.?)(?:\s|$)"),
        ("ENTRY", r"\b(entry[- ]level|entry level|new grad(?:uate)?)\b"),
        ("INTERNSHIP", r"\b(internship|intern)\b"),
    )
    for level, pattern in patterns:
        match = re.search(pattern, folded, re.I)
        if match:
            return {"level": level, "raw": value[match.start():match.end()].strip()}
    return {"level": "UNKNOWN", "raw": None}


def _money_value(value):
    compact = str(value).replace("$", "").replace(",", "").strip()
    multiplier = 1000 if compact.casefold().endswith("k") else 1
    if multiplier != 1:
        compact = compact[:-1]
    try:
        return round(float(compact) * multiplier, 2)
    except ValueError:
        return None


def parse_salary(text, source="UNKNOWN"):
    if source not in SALARY_SOURCES:
        raise ValueError("Salary source is invalid")
    value = str(text or "")
    amount = r"\$\s*\d[\d,]*(?:\.\d+)?\s*[kK]?"
    amount_optional_currency = r"\$?\s*\d[\d,]*(?:\.\d+)?\s*[kK]?"
    period = "HOUR" if re.search(r"(?:/|per\s+)?(?:hour|hr)\b|hourly", value, re.I) else (
        "YEAR" if re.search(r"(?:/|per\s+)?(?:year|yr)\b|annual(?:ly)?", value, re.I) else "UNKNOWN"
    )
    range_match = re.search(fr"(?P<low>{amount})\s*(?:-|–|—|to)\s*(?P<high>{amount_optional_currency})", value, re.I)
    if range_match:
        low = _money_value(range_match.group("low"))
        high = _money_value(range_match.group("high"))
        if low is not None and high is not None and low <= high:
            return {"salary_min": low, "salary_max": high, "salary_period": period, "currency": "USD",
                    "salary_source": source, "salary_raw": range_match.group(0).strip()}
    up_to = re.search(fr"\bup\s+to\s+(?P<amount>{amount})", value, re.I)
    if up_to:
        high = _money_value(up_to.group("amount"))
        return {"salary_min": None, "salary_max": high, "salary_period": period, "currency": "USD",
                "salary_source": source, "salary_raw": up_to.group(0).strip()}
    starting = re.search(fr"\bstarting\s+at\s+(?P<amount>{amount})", value, re.I)
    if starting:
        low = _money_value(starting.group("amount"))
        return {"salary_min": low, "salary_max": None, "salary_period": period, "currency": "USD",
                "salary_source": source, "salary_raw": starting.group(0).strip()}
    plus = re.search(fr"(?P<amount>{amount})\s*\+", value, re.I)
    if plus:
        low = _money_value(plus.group("amount"))
        return {"salary_min": low, "salary_max": None, "salary_period": period, "currency": "USD",
                "salary_source": source, "salary_raw": plus.group(0).strip()}
    single = re.search(fr"(?P<amount>{amount})", value, re.I)
    if single:
        exact = _money_value(single.group("amount"))
        return {"salary_min": exact, "salary_max": exact, "salary_period": period, "currency": "USD",
                "salary_source": source, "salary_raw": single.group(0).strip()}
    return {"salary_min": None, "salary_max": None, "salary_period": "UNKNOWN", "currency": None,
            "salary_source": "UNKNOWN", "salary_raw": None}


_RELATIVE_TIMESTAMP = re.compile(r"(\d{1,6})\s+(minute|hour|day)s?\s+ago", re.I)
_RSS_TIMESTAMP = re.compile(
    r'(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+)?\d{1,2}\s+'
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s+'
    r'\d{2}:\d{2}(?::\d{2})?\s+(?:[+-]\d{4}|GMT|UT)', re.I)


def parse_timestamp(value, *, reference_at=None):
    """Parse source time; relative claims require an explicit observation UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            if _RSS_TIMESTAMP.fullmatch(str(value).strip()):
                try:
                    parsed = parsedate_to_datetime(str(value).strip())
                except (ValueError, TypeError, OverflowError):
                    return None
                # RSS must supply a known offset; do not assign a local zone to
                # an absent/unknown timezone such as RFC's -0000 convention.
                if parsed.tzinfo is None:
                    return None
            else:
                match = _RELATIVE_TIMESTAMP.fullmatch(str(value).strip())
                reference = parse_timestamp(reference_at) if reference_at is not None else None
                if not match or reference is None:
                    return None
                try:
                    return reference - timedelta(**{match[2].lower() + 's': int(match[1])})
                except (ValueError, OverflowError):
                    return None
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def normalize_reported_timestamp(value, *, reference_at, previous=None):
    """Retain the source claim and its first observation, never a moving now()."""
    previous = previous or {}
    relative = _RELATIVE_TIMESTAMP.fullmatch(str(value or '').strip())
    if relative:
        # A mutable age label is not proof of republication. Retain the first
        # normalized claim and its original reference; latest wording remains
        # available in the immutable source posting snapshots.
        prior_value = previous.get('reported_posted_at')
        prior_relative = _RELATIVE_TIMESTAMP.fullmatch(str(prior_value or '').strip())
        prior_reference = (parse_timestamp(previous.get('reported_posted_at_reference_at'))
            or parse_timestamp(previous.get('observed_at'))
            or parse_timestamp(previous.get('last_collected_at'))
            or parse_timestamp(previous.get('first_seen_at')))
        prior = parse_timestamp(prior_value, reference_at=prior_reference)
        if prior is not None:
            precision = previous.get('reported_posted_at_precision')
            if precision not in {'MINUTE', 'HOUR', 'DAY', 'SOURCE'}:
                precision = prior_relative[2].upper() if prior_relative else 'SOURCE'
            return {
                'reported_posted_at': prior.isoformat(),
                # Legacy relative text is the prior claim itself. A saved
                # absolute UTC value does not reveal its original source words;
                # immutable posting snapshots retain those when raw is unknown.
                'reported_posted_at_raw': previous.get('reported_posted_at_raw',
                    prior_value if prior_relative else None),
                'reported_posted_at_precision': precision,
                'reported_posted_at_reference_at': prior_reference.isoformat() if prior_relative else
                    previous.get('reported_posted_at_reference_at'),
            }
    reference = parse_timestamp(reference_at) if relative else None
    parsed = parse_timestamp(value, reference_at=reference)
    return {
        'reported_posted_at': parsed.isoformat() if parsed else None,
        'reported_posted_at_raw': value,
        'reported_posted_at_precision': relative[2].upper() if parsed and relative else (
            'SOURCE' if parsed else 'UNKNOWN'),
        'reported_posted_at_reference_at': reference.isoformat() if relative and reference else None,
    }


def effective_job_timestamp(job):
    attrs = dict(job.get("attributes") or {})
    reported = attrs.get('reported_posted_at')
    if reported is not None and reported != '':
        return parse_timestamp(reported, reference_at=(
            attrs.get('reported_posted_at_reference_at') or attrs.get('first_seen_at')))
    # An invalid supplied claim is unknown, not evidence of a recent posting.
    raw = attrs.get('reported_posted_at_raw')
    if raw is not None and raw != '':
        return None
    return parse_timestamp(attrs.get('first_seen_at'))


def _date_window_bounds(criteria, at):
    window = criteria.get("date_window", "ANY")
    if window == "ANY":
        return None, None
    if window == "LAST_HOUR":
        return at - timedelta(hours=1), at
    if window == "TODAY":
        return at.replace(hour=0, minute=0, second=0, microsecond=0), at
    days = {"LAST_24_HOURS": 1, "LAST_3_DAYS": 3, "LAST_7_DAYS": 7, "LAST_30_DAYS": 30}
    if window in days:
        return at - timedelta(days=days[window]), at
    if window == "CUSTOM":
        return parse_timestamp(criteria.get("date_start")), parse_timestamp(criteria.get("date_end"))
    raise ValueError("Date window is invalid")


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

def normalize_title(value):
    original = " ".join(str(value or "").split())
    folded = original.casefold()
    families = (
        ("SYSTEMS_ADMINISTRATION", "Systems Administrator",
         r"\b(?:windows\s+)?(?:sys(?:tem|tems)?\s+admin(?:istrator)?|sysadmin)\b"),
        ("IT_SUPPORT", "IT Support",
         r"\b(?:help\s*desk|service\s*desk|desktop\s+support|technical\s+support)\b"),
    )
    for family, normalized, pattern in families:
        if re.search(pattern, folded, re.I):
            return {"original_title": original, "normalized_title": normalized, "title_family": family}
    return {"original_title": original, "normalized_title": original, "title_family": None}


def parse_education_requirement(text):
    value = str(text or "")
    # Normalize apostrophes only for recognition; offsets and returned evidence
    # still refer to the original source wording and typography.
    folded = value.translate(str.maketrans({"’": "'", "‘": "'", "ʼ": "'"}))
    degree = re.compile(r"\b(bachelor(?:'s|s)?)(?:\s+degree)?\b|"
                        r"\b(?:college\s+|university\s+)?degree\b", re.I)
    negation = re.compile(r"(?:\b(?:not|never|no\s+longer)|n't)\s+"
                          r"(?:(?:strictly|necessarily|always|required|mandatory|preferred|and|or)\s+)*$", re.I)
    qualification = re.compile(r"\b(?:required|mandatory|preferred|optional)\b", re.I)
    qualifier_prefix = re.compile(r"\s*(?:(?:is|are|also|still)\s+)*"
                                  r"(?:(?:not|never|no longer)\s+)?"
                                  r"(?:(?:strictly|necessarily|always)\s+)*"
                                  r"(?:required|mandatory|preferred)\b", re.I)
    other_subject = re.compile(r"\b(?:clearance|certifications?|experience|skills?|proficiency)\b", re.I)
    alternative = re.compile(r"\s*(?:(?:or|and/or)\s+)?(?:an?\s+)?"
                             r"equivalent\s+(?:experience|combination)\b", re.I)

    # Record bracket depth once. Sentence punctuation and list punctuation inside
    # a balanced descriptor belong to its subject, including nested descriptors.
    levels, statements, stack = [], [], []
    closing = {'(': ')', '[': ']', '{': '}'}
    start, valid = 0, True
    for position, character in enumerate(folded):
        levels.append(len(stack))
        if character in closing:
            stack.append(closing[character])
        elif character in closing.values():
            if not stack or stack.pop() != character:
                valid = False
                stack.clear()
        elif character in '.;\n!?' and not stack:
            if valid:
                statements.append((start, position))
            start, valid = position + 1, True
    if valid and not stack:
        statements.append((start, len(folded)))

    def subject_scopes(suffix, offset, level):
        first_qualifier = qualification.search(suffix)
        subject_end = first_qualifier.start() if first_qualifier else len(suffix)
        equivalent_subject = re.search(r"\b(?:or|and/or)\s+(?:an?\s+)?"
                                       r"equivalent\s+(?:experience|combination)\b", suffix, re.I)
        if equivalent_subject:
            subject_end = min(subject_end, equivalent_subject.start())
        if other_subject.search(suffix[:subject_end]):
            return ['']
        candidates = list(re.finditer(
            r"[;,]|\b(?:and/or|and|or|but|while|whereas|however)\b", suffix, re.I))
        separators = []
        for index, candidate in enumerate(candidates):
            end = candidates[index + 1].start() if index + 1 < len(candidates) else len(suffix)
            # Brackets protect fields, not an explicit change to a non-degree
            # subject such as '(not required; security clearance required)'.
            if (levels[offset + candidate.start()] <= level or
                    (other_subject.search(suffix[candidate.end():end])
                     and not alternative.match(suffix[candidate.end():]))):
                separators.append(candidate)
        scopes, start, field_list = [], 0, False
        for index, separator in enumerate(separators):
            left = suffix[start:separator.start()]
            rest = suffix[separator.end():]
            end = separators[index + 1].start() if index + 1 < len(separators) else len(suffix)
            right = suffix[separator.end():end]
            word = separator.group().lower()
            if alternative.match(rest) and word in {',', 'or', 'and/or'}:
                continue
            continuation = re.match(r"\s*(?:but|while|whereas|however|and|or)\b", rest, re.I)
            if word == ',' and continuation and qualifier_prefix.match(rest[continuation.end():]):
                # Let the conjunction decide whether polarity continues or a
                # contrast starts; its preceding comma is not a new subject.
                continue
            # Explicit non-education subjects cannot borrow the degree's scope.
            if other_subject.search(right):
                scopes.append(left)
                return scopes
            qualified = qualification.search(left)
            if not qualified and not field_list:
                outside = ''.join(character for character_index, character in
                    enumerate(suffix[start:separator.start()], start)
                    if levels[offset + character_index] <= level)
                descriptor = re.sub(r"\b(?:is|are|also|still|not|never|no|longer|"
                                    r"strictly|necessarily|always)\b", '', outside, flags=re.I)
                field_list = bool(re.search(r"\w", descriptor))
                if not descriptor.strip(' :—–-') and word == ',':
                    # A bare comma can introduce an enumerated degree subject,
                    # but a lone new subject (', Python required') cannot.
                    first_qualifier = qualification.search(rest)
                    subject_end = separator.end() + first_qualifier.start() if first_qualifier else len(suffix)
                    field_list = any(separator.end() <= item.start() < subject_end
                                     and item.group().lower() in {',', 'and', 'or', 'and/or'}
                                     and re.search(r"\w", suffix[separator.end():item.start()])
                                     for item in separators[index + 1:])
            if word in {'but', 'while', 'whereas', 'however'}:
                if qualifier_prefix.match(rest):
                    scopes.append(left)
                    start, field_list = separator.end(), False
                    continue
            elif qualifier_prefix.match(rest):
                if word != ',' or not qualified:
                    continue
                scopes.append(left)
                start, field_list = separator.end(), False
                continue
            elif field_list and not qualified:
                continue
            scopes.append(left)
            return scopes
        scopes.append(suffix[start:])
        return scopes

    claims = []
    for clause_start, clause_end in statements:
        clause = folded[clause_start:clause_end]
        mentions = list(degree.finditer(clause))
        for index, mention in enumerate(mentions):
            prefix = clause[:mention.start()]
            negated_mention = re.search(r"\b(?:no|without|not)\s+(?:a\s+)?$", prefix, re.I)
            level = levels[clause_start + mention.start()]
            end = next((item.start() for item in mentions[index + 1:]
                        if levels[clause_start + item.start()] <= level), len(clause))
            scopes = subject_scopes(clause[mention.end():end], clause_start + mention.end(), level)
            strengths = set()
            for scope_index, scope in enumerate(scopes):
                if negated_mention and scope_index == 0:
                    continue
                for strength in re.finditer(r"\b(required|mandatory|preferred)\b", scope, re.I):
                    if not negation.search(scope[:strength.start()]):
                        strengths.add(strength[1].lower())
            preceding = re.search(r"\b(required|mandatory|preferred|require|requires|prefer|prefers)"
                                  r"\s*:?\s*(?:a\s+)?$", prefix, re.I)
            if preceding and not negated_mention and not negation.search(prefix[:preceding.start()]):
                strengths.add({'require': 'required', 'requires': 'required',
                               'prefer': 'preferred', 'prefers': 'preferred'}.get(
                                   preceding[1].lower(), preceding[1].lower()))
            equivalent = re.search(r"\b(?:or|and/or)\s+(?:an?\s+)?equivalent\s+"
                                   r"(?:experience|combination)\b", scopes[0], re.I) if not negated_mention else None
            if equivalent and re.match(r"[\s)\]}]+(?:(?:is|are)\s+)?(?:not|never)\s+"
                                       r"(?:accepted|considered|allowed)\b",
                                       scopes[0][equivalent.end():], re.I):
                equivalent = None
            classification = None
            if mention[1]:
                if equivalent:
                    classification = 'BACHELORS_OR_EQUIVALENT_EXPERIENCE'
                elif strengths & {'required', 'mandatory'}:
                    classification = 'BACHELORS_REQUIRED'
                elif 'preferred' in strengths:
                    classification = 'BACHELORS_PREFERRED'
            elif 'preferred' in strengths:
                classification = 'DEGREE_PREFERRED'
            if classification:
                claims.append((classification, ' '.join(value[clause_start:clause_end].split())))
    # An explicit mandatory claim is not erased by a preferred or alternative
    # claim in another clause. An alternative within the same claim stays distinct.
    priority = ('BACHELORS_REQUIRED', 'BACHELORS_OR_EQUIVALENT_EXPERIENCE',
                'BACHELORS_PREFERRED', 'DEGREE_PREFERRED')
    if claims:
        classification, raw = min(claims, key=lambda claim: priority.index(claim[0]))
        return {'classification': classification, 'raw': raw}
    return {"classification": "NONE_SPECIFIED", "raw": None}


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

def evaluate_search(criteria, job, at=None):
    attrs = dict(job.get("attributes") or {})
    at = parse_timestamp(at) or datetime.now(timezone.utc)
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

    job_skill_ids = set(attrs.get("skill_ids") or [])
    required_skills = set(criteria.get("required_skills") or [])
    for identifier in sorted(required_skills - job_skill_ids):
        reasons.append("REQUIRED_SKILL_MISSING:" + identifier)
    for identifier in sorted(set(criteria.get("excluded_skills") or []) & job_skill_ids):
        reasons.append("EXCLUDED_SKILL:" + identifier)

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

    experience = set(criteria.get("experience_levels") or [])
    if experience and attrs.get("experience_level", "UNKNOWN") not in experience:
        reasons.append("EXPERIENCE_LEVEL_MISMATCH")

    salary_min = attrs.get("salary_min")
    salary_max = attrs.get("salary_max")
    salary_known = salary_min is not None or salary_max is not None
    if criteria.get("salary_known_only") and not salary_known:
        reasons.append("SALARY_UNKNOWN")
    if salary_known and criteria.get("salary_period") and attrs.get("salary_period", "UNKNOWN") != criteria["salary_period"]:
        reasons.append("SALARY_PERIOD_MISMATCH")
    elif salary_known:
        wanted_min = criteria.get("salary_min")
        wanted_max = criteria.get("salary_max")
        if wanted_min is not None and salary_max is not None and salary_max < wanted_min:
            reasons.append("SALARY_BELOW_MINIMUM")
        if wanted_max is not None and salary_min is not None and salary_min > wanted_max:
            reasons.append("SALARY_ABOVE_MAXIMUM")
    elif (criteria.get("salary_min") is not None or criteria.get("salary_max") is not None) and not criteria.get("salary_unknown_allowed", True):
        reasons.append("SALARY_UNKNOWN")

    date_start, date_end = _date_window_bounds(criteria, at)
    if date_start is not None or date_end is not None:
        effective = effective_job_timestamp(job)
        if effective is None:
            reasons.append("DATE_UNKNOWN")
        elif (date_start is not None and effective < date_start) or (date_end is not None and effective > date_end):
            reasons.append("DATE_OUTSIDE_WINDOW")

    clearance = attrs.get("clearance") or {"level": "NONE_STATED"}
    clearance_level = clearance.get("level", "NONE_STATED") if isinstance(clearance, dict) else "NONE_STATED"
    allowed_clearances = set(criteria.get("clearance_levels") or [])
    if allowed_clearances and clearance_level not in allowed_clearances:
        reasons.append("CLEARANCE_LEVEL_MISMATCH")
    if criteria.get("exclude_clearance_required") and clearance_level != "NONE_STATED":
        reasons.append("CLEARANCE_REQUIRED")

    work_authorization = attrs.get("work_authorization") or {"flags": []}
    job_auth_flags = set(work_authorization.get("flags") or []) if isinstance(work_authorization, dict) else set()
    for flag in sorted(set(criteria.get("excluded_work_authorization_flags") or []) & job_auth_flags):
        reasons.append("WORK_AUTHORIZATION_EXCLUDED:" + flag)

    negative_conditions = attrs.get("negative_conditions") or {"flags": [], "travel_percent": None}
    job_negative_flags = set(negative_conditions.get("flags") or []) if isinstance(negative_conditions, dict) else set()
    for flag in sorted(set(criteria.get("excluded_conditions") or []) & job_negative_flags):
        reasons.append("NEGATIVE_CONDITION:" + flag)
    max_travel = criteria.get("max_travel_percent")
    job_travel = negative_conditions.get("travel_percent") if isinstance(negative_conditions, dict) else None
    if max_travel is not None and job_travel is not None and job_travel > max_travel:
        reasons.append("TRAVEL_EXCEEDS_MAXIMUM")

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
    optional_skills = sorted(set(criteria.get("optional_skills") or []) & job_skill_ids)
    return {"matched": not reasons, "reasons": reasons, "optional_matches": optional,
            "optional_skill_matches": optional_skills}

def filter_jobs(rows, criteria):
    results = []
    for row in rows:
        result = evaluate_search(criteria, row)
        if result["matched"]:
            item = dict(row)
            item["search_evidence"] = result
            results.append(item)
    return results
