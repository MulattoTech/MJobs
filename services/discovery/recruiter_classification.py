"""Conservatively classify employer/recruiter status from attributable evidence."""

from collections.abc import Mapping
from math import isfinite


CLASSIFICATIONS = frozenset({
    "DIRECT_EMPLOYER", "STAFFING_AGENCY", "RECRUITER", "UNKNOWN",
})
_SUBJECT_TYPES = frozenset({"JOB", "COMPANY"})
_EVIDENCE_FIELDS = frozenset({
    "classification", "subject_type", "subject_id", "source", "evidence_ref",
    "current", "explicit", "confidence",
})
_MAX_EVIDENCE_ROWS = 100


def _unknown(reason, code, *, evidence=()):
    return {
        "classification": "UNKNOWN",
        "confidence": None,
        "reasons": [reason],
        "evidence": list(evidence),
        "unknowns": [code],
        "advisory": True,
    }


def _required_text(value, *, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value.strip()) <= maximum


def _evidence_key(row):
    return (
        row["subject_type"], row["subject_id"], row["classification"],
        row["source"], row["evidence_ref"], row["confidence"],
    )


def classify_recruiter(evidence, *, expected_job_id, expected_company_id=None):
    """Return an advisory label using only explicit, current, entity-bound claims.

    Each evidence record must name a supported classification, identify the job
    or company it describes, provide source and evidence-reference provenance,
    and assert both currentness and explicitness. No job prose, company name,
    domain, source adapter, or ATS is interpreted here.
    """
    if not _required_text(expected_job_id, maximum=200):
        return _unknown(
            "A valid current job identity is required to bind classification evidence.",
            "JOB_ID_INVALID",
        )
    if evidence is None or evidence == [] or evidence == ():
        return _unknown(
            "No current explicit attributable evidence supports a classification.",
            "NO_CURRENT_ATTRIBUTABLE_EVIDENCE",
        )
    if not isinstance(evidence, (list, tuple)) or len(evidence) > _MAX_EVIDENCE_ROWS:
        return _unknown(
            "Classification evidence is not a supported bounded list.",
            "EVIDENCE_INVALID",
        )

    current_rows = []
    ignored_noncurrent = False
    for raw in evidence:
        if not isinstance(raw, Mapping) or set(raw) != _EVIDENCE_FIELDS:
            return _unknown(
                "A classification claim is missing required attribution or contains unsupported fields.",
                "INVALID_OR_UNATTRIBUTABLE_EVIDENCE",
            )

        classification = raw.get("classification")
        subject_type = raw.get("subject_type")
        subject_id = raw.get("subject_id")
        source = raw.get("source")
        evidence_ref = raw.get("evidence_ref")
        confidence = raw.get("confidence")
        current = raw.get("current")
        explicit = raw.get("explicit")
        if (not isinstance(classification, str) or classification not in CLASSIFICATIONS
                or not isinstance(subject_type, str) or subject_type not in _SUBJECT_TYPES
                or not _required_text(subject_id, maximum=200)
                or not _required_text(source, maximum=128)
                or not _required_text(evidence_ref, maximum=2000)
                or type(current) is not bool or type(explicit) is not bool
                or type(confidence) not in (int, float)
                or not isfinite(confidence) or not 0 <= confidence <= 1):
            return _unknown(
                "A classification claim is malformed or lacks usable provenance.",
                "INVALID_OR_UNATTRIBUTABLE_EVIDENCE",
            )

        expected_subject = expected_job_id if subject_type == "JOB" else expected_company_id
        if (not _required_text(expected_subject, maximum=200)
                or subject_id.strip() != expected_subject.strip()):
            return _unknown(
                "A classification claim is linked to a different or unavailable job/company.",
                "EVIDENCE_SUBJECT_MISMATCH",
            )

        if current is not True or explicit is not True:
            ignored_noncurrent = True
            continue

        current_rows.append({
            "classification": classification,
            "subject_type": subject_type,
            "subject_id": subject_id.strip(),
            "source": source.strip(),
            "evidence_ref": evidence_ref.strip(),
            "current": True,
            "explicit": True,
            "confidence": float(confidence),
        })

    # Exact duplicates should not inflate confidence or change explanation order.
    deduplicated = {_evidence_key(row): row for row in current_rows}
    rows = sorted(deduplicated.values(), key=_evidence_key)
    if not rows:
        code = (
            "STALE_OR_NONEXPLICIT_EVIDENCE_IGNORED" if ignored_noncurrent
            else "NO_CURRENT_ATTRIBUTABLE_EVIDENCE"
        )
        return _unknown(
            "Only stale or non-explicit evidence was supplied; classification remains unknown.",
            code,
        )

    labels = {row["classification"] for row in rows}
    if labels == {"UNKNOWN"}:
        return _unknown(
            "Current attributed evidence does not distinguish the employer relationship.",
            "EXPLICIT_EVIDENCE_INCONCLUSIVE",
            evidence=rows,
        )
    if len(labels) != 1 or "UNKNOWN" in labels:
        return _unknown(
            "Current explicit evidence supports conflicting classifications.",
            "CONFLICTING_CURRENT_CLAIMS",
            evidence=rows,
        )

    classification = next(iter(labels))
    unknowns = ["STALE_OR_NONEXPLICIT_EVIDENCE_IGNORED"] if ignored_noncurrent else []
    return {
        "classification": classification,
        "confidence": round(min(row["confidence"] for row in rows), 3),
        "reasons": [
            f"Current explicit attributable evidence consistently identifies this as {classification}."
        ],
        "evidence": rows,
        "unknowns": unknowns,
        "advisory": True,
    }
