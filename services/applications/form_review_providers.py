"""Safe, local dispatch for value-free ATS form review providers."""
from __future__ import annotations

from collections.abc import Mapping
import re

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.adapters.lever import (
    LeverApplicationAdapter, _known_contact_field,
)
from services.applications.adapters.lever_markup import analyze_lever_markup
from services.applications.form_review import analyze_form_html


_ADAPTERS = {
    "greenhouse": GreenhouseApplicationAdapter,
    "lever": LeverApplicationAdapter,
}
_SAFE_IDENTIFIER = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.:\-\[\]]{0,99}\Z")
_PHONE_LIKE = re.compile(r"\+?\d[\d .()\-]{6,}\d\Z")
_SAFE_TYPES = {
    "text", "email", "tel", "url", "number", "date", "datetime-local", "time",
    "month", "week", "search", "password", "checkbox", "radio", "file", "select",
    "textarea",
}
_SAFE_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


def _provider_name(provider):
    return provider.casefold() if isinstance(provider, str) else None


def normalize_application_route(provider, route_url) -> dict | None:
    """Normalize only routes accepted by the selected ATS adapter."""
    normalized_provider = _provider_name(provider)
    adapter_type = _ADAPTERS.get(normalized_provider)
    if adapter_type is None:
        return None
    return adapter_type().normalize_route({
        "provider": normalized_provider,
        "url": route_url,
    })


def _safe_identifier(value):
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if (not _SAFE_IDENTIFIER.fullmatch(normalized)
            or _PHONE_LIKE.fullmatch(normalized)):
        return None
    return normalized


def _lever_report(analysis) -> dict:
    """Project Lever analyzer output into the same safe report as Greenhouse."""
    if not isinstance(analysis, Mapping):
        return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
    raw_fields = analysis.get("fields")
    if not isinstance(raw_fields, list):
        return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}

    fields = []
    for field in raw_fields:
        if not isinstance(field, Mapping):
            return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
        field_id = _safe_identifier(field.get("id"))
        field_name = _safe_identifier(field.get("name"))
        sensitivity = field.get("sensitivity")
        input_type = field.get("input_type") or field.get("tag")
        normalized_type = input_type.casefold() if isinstance(input_type, str) else ""
        if normalized_type not in _SAFE_TYPES:
            normalized_type = "other"

        if sensitivity == "CREDENTIAL":
            review_reason = "CREDENTIAL_FIELD"
        elif sensitivity == "SENSITIVE":
            review_reason = "SENSITIVE_FIELD"
        elif field.get("ambiguous") is True:
            review_reason = "AMBIGUOUS_FIELD"
        elif ((field.get("id") and field_id is None)
              or (field.get("name") and field_name is None)):
            review_reason = "UNSAFE_IDENTIFIER"
        elif not field_id and not field_name:
            review_reason = "UNIDENTIFIABLE_FIELD"
        elif normalized_type == "other":
            review_reason = "UNSUPPORTED_FIELD_TYPE"
        elif not _known_contact_field(field):
            review_reason = "UNKNOWN_QUESTION"
        else:
            review_reason = None

        fields.append({
            "id": field_id,
            "name": field_name,
            "type": normalized_type,
            "required": field.get("required") is True,
            "review_reason": review_reason,
        })

    analyzer_status = analysis.get("status")
    analyzer_reason = analysis.get("reason")
    if analyzer_status != "PASS":
        reason = (analyzer_reason if isinstance(analyzer_reason, str)
                  and _SAFE_REASON.fullmatch(analyzer_reason) else "FORM_ANALYSIS_UNKNOWN")
        return {"status": "REVIEW_REQUIRED", "reason": reason, "fields": fields}
    if any(field["review_reason"] for field in fields):
        return {"status": "REVIEW_REQUIRED", "reason": "FORM_REQUIRES_HUMAN_REVIEW",
                "fields": fields}
    return {"status": "ANALYZED", "reason": None, "fields": fields}


def analyze_application_form(provider, html, *, route_url) -> dict:
    """Validate a public ATS route and analyze supplied HTML locally.

    This function performs no fetching, storage, browser interaction, or
    submission. Unknown providers and unsupported routes return fixed codes.
    """
    normalized_provider = _provider_name(provider)
    if normalized_provider not in _ADAPTERS:
        return {"status": "REVIEW_REQUIRED", "reason": "PROVIDER_UNSUPPORTED", "fields": []}
    route = normalize_application_route(normalized_provider, route_url)
    if route is None:
        return {"status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": []}

    try:
        if normalized_provider == "greenhouse":
            return analyze_form_html(
                normalized_provider, html, route_url=route["url"],
            )
        # The durable worker supplies the same bounded response bytes as the
        # Greenhouse path. Lever's static markup parser accepts text, so decode
        # locally while preserving fail-closed human review for malformed text.
        markup = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
        return _lever_report(analyze_lever_markup(markup))
    except Exception:
        # Parser exception text is untrusted and may contain supplied markup.
        return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
