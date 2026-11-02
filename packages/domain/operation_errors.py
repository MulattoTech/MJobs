"""Stable public error categories for external operation boundaries."""

OPERATION_ERROR_CATEGORIES = frozenset({
    "NETWORK_ERROR",
    "RATE_LIMITED",
    "SOURCE_CHANGED",
    "SOURCE_BLOCKED",
    "AUTH_REQUIRED",
    "CAPTCHA_REQUIRED",
    "ATS_UNSUPPORTED",
    "FORM_CHANGED",
    "FIELD_UNKNOWN",
    "UPLOAD_FAILED",
    "VALIDATION_FAILED",
    "JOB_CLOSED",
    "SUBMISSION_UNCONFIRMED",
    "AI_ERROR",
    "DOCUMENT_ERROR",
})


def validate_operation_error_category(value):
    """Return a canonical category without echoing rejected input."""
    if type(value) is not str or value not in OPERATION_ERROR_CATEGORIES:
        raise ValueError("OPERATION_ERROR_CATEGORY_INVALID")
    return value


def source_collection_error_category(provider_code):
    """Map a safe source code to the public taxonomy, or fail closed."""
    if type(provider_code) is not str:
        return None
    if provider_code in {"SOURCE_RATE_LIMITED", "SOURCE_HTTP_429"}:
        return "RATE_LIMITED"
    if provider_code == "SOURCE_HTTP_401":
        return "AUTH_REQUIRED"
    if provider_code == "SOURCE_CHANGED":
        return "SOURCE_CHANGED"
    if provider_code in {"SOURCE_REDIRECT_BLOCKED", "SOURCE_BLOCKED", "SOURCE_HTTP_403"}:
        return "SOURCE_BLOCKED"
    if provider_code in {"SOURCE_NETWORK_ERROR", "SOURCE_TIMEOUT", "SOURCE_TIME_LIMIT",
                         "SOURCE_SERVER_ERROR"}:
        return "NETWORK_ERROR"
    http_status = provider_code.removeprefix("SOURCE_HTTP_")
    if len(http_status) == 3 and http_status.startswith("5") and http_status.isdigit():
        return "NETWORK_ERROR"
    return None


def ai_operation_error_category(_diagnostic=None):
    """Map AI diagnostics to the value-free public operation category."""
    # Provider/evaluation details are internal and may contain untrusted text.
    return "AI_ERROR"


_APPLICATION_ERROR_CODES = {
    "UNSUPPORTED_ATS": "ATS_UNSUPPORTED",
    "PROVIDER_UNSUPPORTED": "ATS_UNSUPPORTED",
    "FORM_REVIEW_ROUTE_UNSUPPORTED": "ATS_UNSUPPORTED",
    "CONTROLLED_DRY_RUN_ROUTE_UNSUPPORTED": "ATS_UNSUPPORTED",
    "QUESTION_PREFLIGHT_STALE": "FORM_CHANGED",
    "STALE_PREFLIGHT": "FORM_CHANGED",
    "DYNAMIC_REQUIRED_FIELDS_REVIEW_REQUIRED": "FORM_CHANGED",
    "UNKNOWN_DYNAMIC_REQUIRED_FIELD": "FIELD_UNKNOWN",
    "UNMAPPED_REQUIRED_QUESTION": "FIELD_UNKNOWN",
    "UNKNOWN_QUESTION": "FIELD_UNKNOWN",
    "UNKNOWN_FIELD": "FIELD_UNKNOWN",
    "UNSUPPORTED_FIELD_TYPE": "FIELD_UNKNOWN",
    "UNIDENTIFIABLE_FIELD": "FIELD_UNKNOWN",
    "UploadIntegrityError": "UPLOAD_FAILED",
    "UPLOAD_INTEGRITY_FAILED": "UPLOAD_FAILED",
    "FORM_FIELDS_INVALID": "VALIDATION_FAILED",
}


def operation_error_category(code):
    """Project trusted fixed codes; unknown text and ambiguous states stay unknown."""
    if type(code) is not str:
        return None
    if code in OPERATION_ERROR_CATEGORIES:
        return code
    return _APPLICATION_ERROR_CODES.get(code) or source_collection_error_category(code)
