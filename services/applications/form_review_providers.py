"""Safe, local dispatch for value-free ATS form review providers."""
from __future__ import annotations

from collections.abc import Mapping
import re

from services.applications.adapters.ashby import AshbyApplicationAdapter
from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.adapters.lever import LeverApplicationAdapter
from services.applications.adapters.recruitee import RecruiteeApplicationAdapter
from services.applications.adapters.workday import WorkdayApplicationAdapter
from services.applications.adapters.workday_forms import analyze_workday_form_html
from services.applications.adapters.lever_markup import analyze_lever_markup
from services.applications.form_review import analyze_form_html


_ADAPTERS = {
    "ashby": AshbyApplicationAdapter,
    "greenhouse": GreenhouseApplicationAdapter,
    "lever": LeverApplicationAdapter,
    "recruitee": RecruiteeApplicationAdapter,
    "workday": WorkdayApplicationAdapter,
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


def _contact_provider_report(analysis, adapter_type) -> dict:
    """Project static contact-form analysis using the selected provider policy."""
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
        elif adapter_type.requires_human_review({"status": "PASS", "fields": [field]}):
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


def _lever_report(analysis) -> dict:
    """Project Lever analyzer output using Lever's prompt allowlist."""
    return _contact_provider_report(analysis, LeverApplicationAdapter)


def _recruitee_report(analysis) -> dict:
    """Project Recruitee analyzer output using Recruitee's stricter prompt policy."""
    return _contact_provider_report(analysis, RecruiteeApplicationAdapter)


def _workday_report(analysis) -> dict:
    """Project the Workday-specific analyzer into the durable safe report."""
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
        raw_type = field.get("type")
        normalized_type = raw_type.casefold() if isinstance(raw_type, str) else ""
        if normalized_type not in _SAFE_TYPES:
            normalized_type = "other"

        if field.get("sensitivity") == "CREDENTIAL":
            review_reason = "CREDENTIAL_FIELD"
        elif field.get("sensitivity") == "SENSITIVE":
            review_reason = "SENSITIVE_FIELD"
        elif field.get("sensitivity") not in {"STANDARD", None}:
            review_reason = "UNKNOWN_QUESTION"
        elif ((field.get("id") and field_id is None)
              or (field.get("name") and field_name is None)):
            review_reason = "UNSAFE_IDENTIFIER"
        elif not field_id and not field_name:
            review_reason = "UNIDENTIFIABLE_FIELD"
        elif normalized_type == "other" or field.get("supported") is False:
            review_reason = "UNSUPPORTED_FIELD_TYPE"
        elif field.get("conditional") is not False or field.get("repeatable") is not False:
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

    status = analysis.get("status")
    if status == "UNKNOWN":
        reason = analysis.get("reason")
        if not isinstance(reason, str) or not _SAFE_REASON.fullmatch(reason):
            reason = "FORM_ANALYSIS_UNKNOWN"
        return {"status": "REVIEW_REQUIRED", "reason": reason, "fields": fields}

    if status not in {"PASS", "REVIEW_REQUIRED"}:
        return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
    if any(field.get("conditional") is None or field.get("repeatable") is None
           for field in raw_fields if isinstance(field, Mapping)):
        reason = "WORKDAY_DYNAMIC_SEMANTICS_UNOBSERVED"
    elif any(field.get("conditional") is True or field.get("repeatable") is True
             for field in raw_fields if isinstance(field, Mapping)):
        reason = "WORKDAY_DYNAMIC_FIELD_PRESENT"
    else:
        reason = "MANUAL_REVIEW_REQUIRED"
    # This provider is deliberately review-only even when static metadata is clear.
    return {"status": "REVIEW_REQUIRED", "reason": reason, "fields": fields}


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
        if normalized_provider == "ashby":
            return AshbyApplicationAdapter().analyze_form(html, route_url=route["url"])
        if normalized_provider == "workday":
            return _workday_report(analyze_workday_form_html(html))
        # The durable worker supplies bounded response bytes. Lever's static
        # markup parser accepts text, so decode locally and fail closed on malformed text.
        markup = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
        analysis = analyze_lever_markup(markup)
        if normalized_provider == "recruitee":
            return _recruitee_report(analysis)
        return _lever_report(analysis)
    except Exception:
        # Parser exception text is untrusted and may contain supplied markup.
        return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
