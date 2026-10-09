"""Deterministic, advisory application-priority explanations."""

from collections.abc import Mapping
from datetime import datetime, timezone
from math import isfinite


_FACTORS = (
    ("match", "Match", "match_score", 25),
    ("posting_age", "Posting age", "posted_at", 15),
    ("salary", "Salary", "salary_status", 15),
    ("company_preference", "Company preference", "company_preference", 10),
    ("direct_employer_availability", "Direct-employer availability",
     "direct_employer_availability", 10),
    ("application_complexity", "Application complexity", "application_complexity", 10),
    ("source_freshness", "Source freshness", "source_freshness_at", 10),
    ("expiration_risk", "Expiration risk", "expires_at", 5),
)
_SOURCE_BY_KEY = {
    "match": "signals.match_score",
    "posting_age": "signals.posted_at",
    "salary": "signals.salary_status",
    "company_preference": "signals.company_preference",
    "direct_employer_availability": "signals.direct_employer_availability",
    "application_complexity": "signals.application_complexity",
    "source_freshness": "signals.source_freshness_at",
    "expiration_risk": "signals.expires_at",
    "work_arrangement": "match.components.fit_factors[work_arrangement]",
}
_OPTIONAL_FACTORS = (
    ("work_arrangement", "Work arrangement", "work_arrangement_status", 10),
)
_PREFERENCE_FACTOR = {
    "overall": "match",
    "overall_fit": "match",
    "seniority": "match",
    "technology": "match",
    "location": "work_arrangement",
    "salary": "salary",
    "company": "company_preference",
}
_PREFERENCE_WEIGHT_STEP = 5


def _reviewed_weight_overrides(applied_preferences, factors):
    if applied_preferences is None:
        return {}, {}
    if not isinstance(applied_preferences, (list, tuple)):
        raise ValueError("Applied preferences must be a list")
    factor_keys = {row[0] for row in factors}
    latest_by_factor = {}
    for item in applied_preferences:
        if not isinstance(item, Mapping):
            raise ValueError("Applied preference must be an object")
        dimension = item.get("dimension")
        direction = item.get("direction")
        preference_id = item.get("id")
        if (dimension not in _PREFERENCE_FACTOR
                or direction not in {"INCREASE", "DECREASE"}
                or not isinstance(preference_id, str) or not preference_id):
            raise ValueError("Applied preference is invalid")
        factor = _PREFERENCE_FACTOR[dimension]
        if factor not in factor_keys:
            continue
        created_at = _timestamp(item.get("created_at"))
        if created_at is None:
            raise ValueError("Applied preference timestamp is invalid")
        candidate = (created_at, preference_id, direction, dimension)
        if factor not in latest_by_factor or candidate[:2] > latest_by_factor[factor][:2]:
            latest_by_factor[factor] = candidate
    base_weights = {row[0]: row[3] for row in factors}
    return {
        key: (
            min(100, base_weights[key] + _PREFERENCE_WEIGHT_STEP)
            if value[2] == "INCREASE"
            else max(0, base_weights[key] - _PREFERENCE_WEIGHT_STEP)
        )
        for key, value in latest_by_factor.items()
    }, {
        key: {"dimension": value[3], "direction": value[2]}
        for key, value in latest_by_factor.items()
    }


def _timestamp(value):
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _match_score(value):
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 100:
        return None
    return float(value), f"Stored match score is {float(value):.2f} out of 100."


def _categorical_score(value, scores):
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    score = scores.get(normalized)
    if score is None:
        return None
    return float(score), f"The explicit {normalized.replace('_', ' ').lower()} signal maps to {score} out of 100."


def _age_score(value, now, *, dimension):
    observed = _timestamp(value)
    if observed is None or now is None:
        return None
    hours = (now - observed).total_seconds() / 3600
    if hours < 0:
        return None
    if dimension == "posting_age":
        bands = ((24, 100), (168, 80), (336, 60), (720, 40))
        reason = f"The reported posting date is {hours / 24:.1f} days old."
    else:
        bands = ((24, 100), (72, 80), (168, 60), (720, 40))
        reason = f"The source was last observed {hours:.1f} hours ago."
    score = next((points for bound, points in bands if hours <= bound), 20)
    return float(score), reason


def _expiration_score(value, now):
    expires = _timestamp(value)
    if expires is None or now is None:
        return None
    hours = (expires - now).total_seconds() / 3600
    if hours <= 0:
        return 0.0, "The supplied expiration time has passed; this is an urgency signal, not an availability or eligibility decision."
    bands = ((24, 100), (72, 80), (168, 60), (720, 40))
    score = next((points for bound, points in bands if hours <= bound), 20)
    return float(score), f"The supplied expiration time is {hours:.1f} hours away."


def _factor(row, *, status, score=None, contribution=None, detail=None):
    key, label, _, weight = row
    if status == "UNKNOWN":
        explanation = f"{label} is unknown because a valid explicit signal was not supplied; it contributes no priority points."
    else:
        explanation = (
            f"{detail} This factor has a {weight}% weight and contributes "
            f"{contribution:.2f} weighted priority points."
        )
    return {
        "key": key,
        "label": label,
        "score": score,
        "contribution": contribution,
        "status": status,
        "explanation": explanation,
        "source": _SOURCE_BY_KEY[key],
    }


def explain_application_priority(signals, *, now, applied_preferences=None):
    """Explain relative handling priority from caller-supplied known signals.

    `now` must be timezone-aware for posting-age, source-freshness and expiration
    comparisons. Missing or invalid inputs remain UNKNOWN and are omitted from
    the weighted mean, with their lost weight reflected in coverage_percent.
    This result is advisory and never changes the match score or eligibility.
    """
    signals = signals if isinstance(signals, Mapping) else {}
    current_time = _timestamp(now)
    extractors = {
        "match": lambda value: _match_score(value),
        "posting_age": lambda value: _age_score(value, current_time, dimension="posting_age"),
        "salary": lambda value: _categorical_score(value, {
            "ALIGNS": 100, "NO_DIRECT_MATCH": 20,
        }),
        "company_preference": lambda value: _categorical_score(value, {
            "PREFERRED": 100, "NEUTRAL": 60, "DISFAVORED": 20,
        }),
        "direct_employer_availability": lambda value: _categorical_score(value, {
            "DIRECT_EMPLOYER": 100, "STAFFING_AGENCY": 75, "RECRUITER": 60,
        }),
        "application_complexity": lambda value: _categorical_score(value, {
            "LOW": 100, "MEDIUM": 60, "HIGH": 20,
        }),
        "source_freshness": lambda value: _age_score(value, current_time, dimension="source_freshness"),
        "expiration_risk": lambda value: _expiration_score(value, current_time),
        "work_arrangement": lambda value: _categorical_score(value, {
            "ALIGNS": 100, "NO_DIRECT_MATCH": 20,
        }),
    }

    factors_to_score = list(_FACTORS)
    if "work_arrangement_status" in signals:
        factors_to_score.extend(_OPTIONAL_FACTORS)
    adjusted_weights, adjusted_by_factor = _reviewed_weight_overrides(
        applied_preferences, factors_to_score,
    )

    factors = []
    unknowns = []
    weighted_total = 0.0
    known_weight = 0
    for row in factors_to_score:
        key, _, signal_key, base_weight = row
        weight = adjusted_weights.get(key, base_weight)
        factor_row = (row[0], row[1], row[2], weight)
        result = extractors[key](signals.get(signal_key))
        if result is None:
            unknowns.append(key)
            factors.append(_factor(factor_row, status="UNKNOWN"))
            continue
        score, detail = result
        preference = adjusted_by_factor.get(key)
        if preference:
            detail += (
                f" A user-reviewed {preference['dimension']} preference adjusted this advisory factor's "
                f"weight by 5 points ({preference['direction'].lower()})."
            )
        contribution = round(weight * score / 100, 2)
        weighted_total += contribution
        known_weight += weight
        factors.append(_factor(
            factor_row, status="KNOWN", score=score, contribution=contribution, detail=detail,
        ))

    priority_score = round(weighted_total * 100 / known_weight, 2) if known_weight else None
    priority_band = (
        "UNKNOWN" if priority_score is None else
        "HIGH" if priority_score >= 75 else
        "NORMAL" if priority_score >= 45 else
        "LOW"
    )
    return {
        "priority_score": priority_score,
        "priority_band": priority_band,
        "coverage_percent": known_weight,
        "factors": factors,
        "unknowns": unknowns,
        "advisory": True,
        "affects_match_score": False,
    }
