"""Deterministic, bounded source-quality measurements."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from numbers import Integral, Real


COMPONENTS = (
    "freshness", "completeness", "availability", "parser_reliability",
    "salary_completeness", "application_link_accuracy", "historical_uptime",
)
WINDOW_DAYS = 30
MAX_OBSERVATIONS = 200


def _timestamp(value, field):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO timestamp") from exc
    else:
        raise ValueError(f"{field} must be an aware datetime or ISO timestamp")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


def _number(value, field, *, integer=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Integral if integer else Real)):
        raise ValueError(f"{field} must be a finite {'integer' if integer else 'number'}")
    value = int(value) if integer else float(value)
    if value < 0 or (not integer and not math.isfinite(value)):
        raise ValueError(f"{field} must be a finite nonnegative {'integer' if integer else 'number'}")
    return value


def _observation(row):
    if not isinstance(row, dict):
        raise ValueError("Each observation must be an object")
    if "observed_at" not in row or "succeeded" not in row or type(row["succeeded"]) is not bool:
        raise ValueError("Each observation requires an observed_at timestamp and boolean succeeded")
    values = {"observed_at": _timestamp(row["observed_at"], "observed_at"), "succeeded": row["succeeded"]}
    for field in ("available",):
        value = row.get(field)
        if value is not None and type(value) is not bool:
            raise ValueError(f"{field} must be boolean or null")
        values[field] = value
    for field in ("enumerated", "accepted", "salary_known", "links_checked", "links_accurate"):
        values[field] = _number(row.get(field), field, integer=True)
    values["completeness_sum"] = _number(row.get("completeness_sum"), "completeness_sum")
    if values["accepted"] is not None and values["enumerated"] is None:
        raise ValueError("accepted requires enumerated")
    if values["accepted"] is not None and values["enumerated"] is not None and values["accepted"] > values["enumerated"]:
        raise ValueError("accepted cannot exceed enumerated")
    if values["completeness_sum"] is not None and values["accepted"] is None:
        raise ValueError("completeness_sum requires accepted")
    if values["salary_known"] is not None and values["accepted"] is None:
        raise ValueError("salary_known requires accepted")
    if values["links_checked"] is not None and values["accepted"] is None:
        raise ValueError("links_checked requires accepted")
    if values["links_checked"] is not None and values["accepted"] is not None and values["links_checked"] > values["accepted"]:
        raise ValueError("links_checked cannot exceed accepted")
    if values["links_accurate"] is not None and values["links_checked"] is None:
        raise ValueError("links_accurate requires links_checked")
    if values["salary_known"] is not None and values["accepted"] is not None and values["salary_known"] > values["accepted"]:
        raise ValueError("salary_known cannot exceed accepted")
    if values["links_accurate"] is not None and values["links_checked"] is not None and values["links_accurate"] > values["links_checked"]:
        raise ValueError("links_accurate cannot exceed links_checked")
    if values["completeness_sum"] is not None and values["accepted"] is not None and values["completeness_sum"] > values["accepted"]:
        raise ValueError("completeness_sum cannot exceed accepted")
    return values


def quality_score(observations, *, at):
    """Return seven bounded source-quality components over the trailing 30 days."""
    reference = _timestamp(at, "at")
    if observations is None or isinstance(observations, (str, bytes, dict)):
        raise ValueError("observations must be an iterable of objects")
    parsed = [_observation(row) for row in observations]
    start = reference - timedelta(days=WINDOW_DAYS)
    future = sum(row["observed_at"] > reference for row in parsed)
    old = sum(row["observed_at"] < start for row in parsed)
    window = sorted((row for row in parsed if start <= row["observed_at"] <= reference),
                    key=lambda row: row["observed_at"], reverse=True)[:MAX_OBSERVATIONS]
    components = {name: None for name in COMPONENTS}
    counts = {name: 0 for name in COMPONENTS}
    successful = [row for row in window if row["succeeded"]]
    if successful:
        age = (reference - successful[0]["observed_at"]).total_seconds() / (WINDOW_DAYS * 86400)
        components["freshness"] = max(0.0, min(100.0, (1.0 - age) * 100.0))
        counts["freshness"] = len(successful)
    availability_rows = sorted((row for row in window if row["available"] is not None),
                               key=lambda row: (row["observed_at"], row["available"] is False), reverse=True)
    availability = [row["available"] for row in availability_rows]
    if availability:
        components["availability"] = 100.0 if availability[0] else 0.0
        components["historical_uptime"] = sum(availability) / len(availability) * 100.0
        counts["availability"] = len(availability)
        counts["historical_uptime"] = len(availability)
    enum_rows = [row for row in window if row["enumerated"] is not None and row["accepted"] is not None]
    enum_total = sum(row["enumerated"] for row in enum_rows)
    if enum_total:
        components["parser_reliability"] = sum(row["accepted"] for row in enum_rows) / enum_total * 100.0
        counts["parser_reliability"] = len(enum_rows)
    complete_rows = [row for row in window if row["accepted"] and row["completeness_sum"] is not None]
    accepted_total = sum(row["accepted"] for row in complete_rows)
    if accepted_total:
        components["completeness"] = sum(row["completeness_sum"] for row in complete_rows) / accepted_total * 100.0
        counts["completeness"] = len(complete_rows)
    salary_rows = [row for row in window if row["accepted"] and row["salary_known"] is not None]
    salary_total = sum(row["accepted"] for row in salary_rows)
    if salary_total:
        components["salary_completeness"] = sum(row["salary_known"] for row in salary_rows) / salary_total * 100.0
        counts["salary_completeness"] = len(salary_rows)
    link_rows = [row for row in window if row["links_checked"] and row["links_accurate"] is not None]
    checked_total = sum(row["links_checked"] for row in link_rows)
    if checked_total:
        components["application_link_accuracy"] = sum(row["links_accurate"] for row in link_rows) / checked_total * 100.0
        counts["application_link_accuracy"] = len(link_rows)
    observed = [value for value in components.values() if value is not None]
    limitations = [
        "Freshness is collection age since the last succeeded collection, not job-posted age.",
        "Completeness measures observed field presence only where accepted counts and completeness sums exist.",
        "Historical uptime is observed attempt availability, not continuous uptime.",
        "Application-link accuracy is unknown when no links were explicitly sampled.",
        "equal-weight score and coverage describe measurement coverage, not authority or probability.",
    ]
    if not window:
        limitations.insert(0, "No observations in the trailing 30-day window.")
    return {
        "components": components,
        "score": sum(observed) / len(observed) if observed else None,
        "coverage": len(observed) / len(COMPONENTS) * 100.0,
        "sample_counts": counts,
        "weights": {name: 1.0 for name in COMPONENTS},
        "window": {"at": reference.isoformat(), "start": start.isoformat(), "days": WINDOW_DAYS,
                   "observation_count": len(window), "excluded_future": future,
                   "excluded_outside_days": old},
        "limitations": limitations,
    }
