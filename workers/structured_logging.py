"""Privacy-bounded JSON records for durable worker operation outcomes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import sys


_LOGGER = logging.getLogger("mjobs.operations")
_OPERATION_KINDS = {
    "DISCOVER", "COMPANY_DISCOVER", "COMPANY_SOURCE_REFRESH", "PREPARE", "APPLY_MOCK",
}
_RESULTS = {"COMPLETED", "BLOCKED", "CANCELLED", "DEAD_LETTERED"}
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SECRET_SHAPED_IDENTIFIER = re.compile(
    r"(?:^|[._:-])(?:api[_-]?key|access[_-]?(?:key|token)|secret|token|password|credential)(?:$|[._:-])",
    re.IGNORECASE,
)
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")


def _safe_identifier(value):
    return value if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) else None


def _safe_discovery_source_id(operation):
    """Return only an explicit persisted source identifier for a discovery kind."""
    params = operation.get("params")
    if not isinstance(params, dict):
        return None

    kind = operation.get("kind")
    if kind == "DISCOVER" or kind == "COMPANY_SOURCE_REFRESH":
        value = params.get("source_id")
    elif kind == "COMPANY_DISCOVER":
        value = params.get("monitoring_source_id")
    else:
        return None

    identifier = _safe_identifier(value)
    if identifier and not _SECRET_SHAPED_IDENTIFIER.search(identifier):
        return identifier
    return None


def _safe_error_code(value):
    return value if isinstance(value, str) and _SAFE_ERROR_CODE.fullmatch(value) else None


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
    params = operation.get("params")
    source_id = _safe_discovery_source_id(operation)
    if source_id:
        record["source_id"] = source_id

    if kind == "PREPARE" and isinstance(params, dict):
        job_id = _safe_identifier(params.get("job_id"))
        if job_id:
            record["job_id"] = job_id
    elif kind == "COMPANY_DISCOVER":
        company_id = _safe_identifier(operation.get("entity_id"))
        if company_id:
            record["company_id"] = company_id
    elif kind == "APPLY_MOCK":
        application_id = _safe_identifier(operation.get("entity_id"))
        if application_id:
            record["application_id"] = application_id

    safe_code = _safe_error_code(error_code)
    if safe_code:
        record["error_code"] = safe_code

    _LOGGER.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
