"""Stable canonical job/read representations independent of source adapters."""
from __future__ import annotations

import re


def normalize_company_name(value):
    """Normalize display/search spelling without asserting company identity."""
    text = " ".join(str(value or "").split())
    folded = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    suffixes = {"inc", "incorporated", "corp", "corporation", "llc", "ltd", "limited"}
    parts = folded.split()
    while parts and parts[-1] in suffixes:
        parts.pop()
    return " ".join(parts)


def canonical_job_view(row):
    """Return one provider-neutral representation while retaining compatibility fields."""
    data = dict(row)
    attrs = dict(data.get("attributes") or {})
    canonical = {
        "canonical_job_id": data.get("id"),
        "title": data.get("title"),
        "normalized_title": attrs.get("normalized_title") or data.get("title"),
        "title_family": attrs.get("title_family"),
        "company_id": data.get("company_id"),
        "company_name": data.get("company"),
        "normalized_company_name": normalize_company_name(data.get("company")),
        "description_html": attrs.get("description_html"),
        "description_text": data.get("description") or "",
        "responsibilities": attrs.get("responsibilities") or [],
        "required_qualifications": attrs.get("required_qualifications") or [],
        "preferred_qualifications": attrs.get("preferred_qualifications") or [],
        "skills": list(data.get("skills") or []),
        "employment_type": attrs.get("employment_type", "UNKNOWN"),
        "experience_level": attrs.get("experience_level", "UNKNOWN"),
        "salary_min": attrs.get("salary_min"),
        "salary_max": attrs.get("salary_max"),
        "salary_currency": attrs.get("salary_currency"),
        "salary_period": attrs.get("salary_period", "UNKNOWN"),
        "salary_source": attrs.get("salary_source", "UNKNOWN"),
        "location_text": data.get("location") or "Unknown",
        "city": attrs.get("city"),
        "state": attrs.get("state"),
        "country": attrs.get("country"),
        "remote_type": data.get("remote_type", "UNKNOWN"),
        "remote_regions": list(attrs.get("remote_regions") or []),
        "reported_posted_at": attrs.get("reported_posted_at"),
        "first_seen_at": attrs.get("first_seen_at"),
        "last_seen_at": attrs.get("last_seen_at"),
        "expires_at": attrs.get("expires_at"),
        "status": attrs.get("status", "ACTIVE"),
    }
    # Existing clients still consume id/description/location/attributes and match fields.
    data.update(canonical)
    return data


def raw_posting_view(row):
    """Expose provenance fields separately from the untouched provider payload."""
    data = dict(row)
    return {
        **data,
        "fetch_timestamp": data.get("fetched_at"),
        "raw_structured_payload": data.get("raw"),
        "source_url": data.get("source_url") or data.get("source"),
        "application_url": data.get("application_url") or "",
    }
