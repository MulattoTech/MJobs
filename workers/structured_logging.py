"""Privacy-bounded JSON records for durable worker operation outcomes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import sys

from services.ats_detection.detector import KNOWN_ATS_PROVIDERS


_LOGGER = logging.getLogger("mjobs.operations")
_OPERATION_KINDS = {
    "DISCOVER", "COMPANY_DISCOVER", "COMPANY_SOURCE_REFRESH", "PREPARE", "APPLY_MOCK",
    "RESUME_EXTRACT_QUEUED", "UPLOADED_RESUME_COMPARE_QUEUED", "AI_EVALUATE_QUEUED",
    "APPLICATION_FORM_REVIEW", "APPLICATION_DRY_RUN",
}
_RESULTS = {"COMPLETED", "BLOCKED", "CANCELLED", "DEAD_LETTERED"}
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SECRET_SHAPED_IDENTIFIER = re.compile(
    r"(?:^|[._:-])(?:api[_-]?key|access[_-]?(?:key|token)|secret|token|password|credential)(?:$|[._:-])",
    re.IGNORECASE,
)
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_SAFE_ATS_PROVIDERS = KNOWN_ATS_PROVIDERS | {"mock-ats"}


def _safe_identifier(value):
    return value if (isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value)
                     and not _SECRET_SHAPED_IDENTIFIER.search(value)) else None


def _safe_error_code(value):
    return value if (isinstance(value, str) and _SAFE_ERROR_CODE.fullmatch(value)
                     and not _SECRET_SHAPED_IDENTIFIER.search(value)) else None


def _safe_ats_provider(value):
    return value if isinstance(value, str) and value in _SAFE_ATS_PROVIDERS else None


# Exact scalar field sets by operation kind; payloads and batch arrays never pass.
_JOB_FIELDS = {"job_id", "posting_id", "company_id", "application_id", "source_id", "source", "ats_provider"}
_SOURCE_FIELDS = {"source_id", "source", "company_id", "ats_provider"}
_CONTEXT_FIELDS = {
    "PREPARE": _JOB_FIELDS,
    "APPLY_MOCK": _JOB_FIELDS,
    "APPLICATION_FORM_REVIEW": _JOB_FIELDS,
    "APPLICATION_DRY_RUN": _JOB_FIELDS,
    "UPLOADED_RESUME_COMPARE_QUEUED": _JOB_FIELDS,
    "DISCOVER": _SOURCE_FIELDS,
    "COMPANY_DISCOVER": _JOB_FIELDS - {"application_id"},
    "COMPANY_SOURCE_REFRESH": _SOURCE_FIELDS,
}
_SAFE_SOURCES = _SAFE_ATS_PROVIDERS | {
    "mock-board", "mock-employer", "remotive", "wwr", "remoteok", "remote_ok",
    "rss", "sitemap", "html", "json", "public-api",
}


def safe_operation_context(kind, context):
    """Filter a persisted projection again at the JSON boundary."""
    if not isinstance(context, dict):
        return {}
    result = {}
    for field in _CONTEXT_FIELDS.get(kind, ()):
        value = context.get(field)
        if field == "ats_provider":
            value = _safe_ats_provider(value)
        elif field == "source":
            value = value if isinstance(value, str) and value in _SAFE_SOURCES else None
        else:
            value = _safe_identifier(value)
        if value is not None:
            result[field] = value
    return result


def configure_operation_logger(stream=None):
    """Write operation events as JSON-only lines, separate from RQ text logs."""
    for handler in tuple(_LOGGER.handlers):
        _LOGGER.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False


def log_operation_finished(operation, result, error_code, duration_seconds):
    """Log fixed fields only; never serialize params or exception messages."""
    kind = operation.get("kind")
    if kind not in _OPERATION_KINDS or result not in _RESULTS:
        return

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": "operation_finished",
        "level": "INFO",
        "operation_id": _safe_identifier(operation.get("id")),
        "operation_kind": kind,
        "worker": "mjobs-worker",
        "duration_ms": max(0, int(duration_seconds * 1000)),
        "result": result,
    }
    context = safe_operation_context(kind, operation.get("log_context"))
    if "ats_provider" in context:
        context["ats"] = context.pop("ats_provider")
    record.update(context)

    safe_code = _safe_error_code(error_code)
    if safe_code:
        record["error_code"] = safe_code

    _LOGGER.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
