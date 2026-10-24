"""Confidence, boilerplate reduction, and first-party precedence for duplicate review."""
from __future__ import annotations

from dataclasses import dataclass
import re

from services.dedupe.signals import duplicate_candidate_evidence, normalize_description


BOILERPLATE_PATTERNS = (
    r"equal opportunity employer",
    r"eeo employer",
    r"without regard to race",
    r"reasonable accommodation",
    r"we offer (?:a )?competitive (?:benefits|compensation)",
    r"benefits (?:include|may include)",
    r"comprehensive benefits package",
)
SOURCE_AUTHORITY = {
    "UNKNOWN": 0,
    "AGGREGATOR": 10,
    "DIRECT_EMPLOYER": 20,
    "VERIFIED_ATS": 30,
    "VERIFIED_EMPLOYER": 40,
}


@dataclass(frozen=True)
class DedupeThresholds:
    auto_merge_min: float = 0.95
    review_min: float = 0.65
    near_identical_description: float = 0.90
    high_description_similarity: float = 0.72

    def __post_init__(self):
        values = (
            self.auto_merge_min,
            self.review_min,
            self.near_identical_description,
            self.high_description_similarity,
        )
        if any(type(value) not in (int, float) or not 0 <= value <= 1 for value in values):
            raise ValueError("DEDUPE_THRESHOLD_INVALID")
        if self.review_min > self.auto_merge_min:
            raise ValueError("DEDUPE_THRESHOLD_ORDER_INVALID")


def reduce_boilerplate(value):
    """Return comparison text plus evidence about removed standard-employer prose."""
    text = str(value or "")
    sentences = re.split(r"(?<=[.!?])\s+|[\r\n]+", text)
    kept, removed = [], []
    for sentence in sentences:
        stripped = sentence.strip()
        if not stripped:
            continue
        folded = normalize_description(stripped)
        if any(re.search(pattern, folded) for pattern in BOILERPLATE_PATTERNS):
            removed.append(stripped)
        else:
            kept.append(stripped)
    return {
        "comparison_text": " ".join(kept),
        "removed": removed,
        "removed_count": len(removed),
    }


def _location_key(job):
    attrs = job.get("attributes") or {}
    values = (
        attrs.get("country"),
        attrs.get("state"),
        attrs.get("city"),
        job.get("location"),
        job.get("remote_type"),
    )
    return tuple(str(value or "").strip().casefold() for value in values)


def duplicate_confidence(left, right, thresholds=None):
    thresholds = thresholds or DedupeThresholds()
    left_copy, right_copy = dict(left), dict(right)
    left_boilerplate = reduce_boilerplate(left.get("description"))
    right_boilerplate = reduce_boilerplate(right.get("description"))
    left_copy["description"] = left_boilerplate["comparison_text"]
    right_copy["description"] = right_boilerplate["comparison_text"]
    evidence = duplicate_candidate_evidence(left_copy, right_copy)

    strong = set(evidence["strong_signals"])
    if (evidence["strong_match"]
            and ("ATS_TENANT_REQUISITION" in strong or "EMPLOYER_REQUISITION" in strong)):
        score, reason = 1.0, "EXACT_REQUISITION"
    elif evidence["strong_match"] and "SOURCE_JOB_RELATIONSHIP" in strong:
        score, reason = 1.0, "EXACT_SOURCE_RELATIONSHIP"
    elif "APPLICATION_URL" in strong and evidence["company"]["same_company"]:
        score, reason = 0.97, "SAME_APPLICATION_ROUTE"
    elif (evidence["company"]["same_company"] and evidence["title"]["equal"]
          and evidence["description"]["exact"]):
        score, reason = 0.94, "SAME_COMPANY_TITLE_EXACT_DESCRIPTION"
    elif (evidence["company"]["same_company"] and evidence["title"]["equal"]
          and _location_key(left) == _location_key(right)
          and evidence["description"]["similarity"] >= thresholds.high_description_similarity):
        score, reason = 0.87, "SAME_COMPANY_TITLE_LOCATION_HIGH_DESCRIPTION"
    elif (evidence["company"]["same_company"] and evidence["title"]["equal"]
          and evidence["description"]["similarity"] >= thresholds.review_min):
        score, reason = max(0.65, round(evidence["description"]["similarity"], 2)), "POSSIBLE_DUPLICATE"
    else:
        score, reason = min(0.64, round(evidence["description"]["similarity"] * 0.64, 2)), "INSUFFICIENT_EVIDENCE"

    if score >= thresholds.auto_merge_min:
        disposition = "AUTO_MERGE_ELIGIBLE"
    elif score >= thresholds.review_min:
        disposition = "DUPLICATE_REVIEW_REQUIRED"
    else:
        disposition = "SEPARATE"
    return {
        "confidence": round(score, 2),
        "disposition": disposition,
        "reason": reason,
        "thresholds": {
            "auto_merge_min": thresholds.auto_merge_min,
            "review_min": thresholds.review_min,
        },
        "signals": evidence,
        "boilerplate": {
            "left_removed": left_boilerplate["removed_count"],
            "right_removed": right_boilerplate["removed_count"],
        },
    }


def reconcile_authoritative_value(records):
    """Choose current normalized value without discarding disagreeing source provenance."""
    if not isinstance(records, list) or not records:
        raise ValueError("SOURCE_VALUES_REQUIRED")
    normalized = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError("SOURCE_VALUE_INVALID")
        authority = str(record.get("authority") or "UNKNOWN").upper()
        if authority not in SOURCE_AUTHORITY:
            raise ValueError("SOURCE_AUTHORITY_INVALID")
        normalized.append({
            "value": record.get("value"),
            "authority": authority,
            "source": str(record.get("source") or ""),
            "observed_at": str(record.get("observed_at") or ""),
            "rank": SOURCE_AUTHORITY[authority],
            "index": index,
        })
    winner = max(normalized, key=lambda row: (row["rank"], row["observed_at"], -row["index"]))
    return {
        "value": winner["value"],
        "authority": winner["authority"],
        "source": winner["source"],
        "provenance": [{key: row[key] for key in ("value", "authority", "source", "observed_at")} for row in normalized],
        "conflict": len({repr(row["value"]) for row in normalized}) > 1,
    }
