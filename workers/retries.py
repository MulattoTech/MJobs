"""Conservative retry scheduling for reversible, read-only discovery operations."""
from datetime import datetime, timedelta
import math
import re

import httpx
from sqlalchemy import select

from packages.persistence import models as m
from services.company_discovery.public_client import PublicFetchError, HTTPStatusError
from services.discovery.sources.greenhouse import SourceError
from workers.operations import MAX_ATTEMPTS


_SAFE_KINDS = {"DISCOVER", "COMPANY_DISCOVER"}
_TRANSIENT_FETCH_CODES = {"DNS_TIMEOUT", "DNS_BUSY", "DNS_FAILED", "FETCH_DEADLINE"}


def discovery_retry_plan(connection, operation, failure, timestamp):
    """Return a persisted retry deadline, or None when operator review is safer.

    This deliberately excludes applications, documents and inline AI. A lost
    application acknowledgment must follow its reconciliation state machine.
    Exceptions are classified by type/code, never provider-controlled messages.
    """
    # Monitoring already owns its bounded backoff and rediscovery chain.
    # A second retry controller would bypass that source's retry policy.
    if (operation["kind"] not in _SAFE_KINDS or operation["params"].get("monitoring_source_id")
            or operation["attempt_count"] >= MAX_ATTEMPTS):
        return None
    retry_seconds = 0
    if isinstance(failure, SourceError):
        code = failure.code
        if code not in {"SOURCE_RATE_LIMITED", "SOURCE_TIMEOUT", "SOURCE_NETWORK_ERROR", "SOURCE_TIME_LIMIT", "SOURCE_SERVER_ERROR"} and not re.fullmatch(r"SOURCE_HTTP_5\d\d", code):
            return None
        retry_seconds = failure.retry_seconds
    elif isinstance(failure, HTTPStatusError):
        if not 500 <= failure.status_code <= 599:
            return None
        code = "HTTP_STATUS_ERROR"
    elif isinstance(failure, PublicFetchError):
        if failure.code not in _TRANSIENT_FETCH_CODES:
            return None
        code = failure.code
    elif isinstance(failure, (httpx.TimeoutException, httpx.NetworkError)):
        code = "NETWORK_ERROR"
    else:
        return None
    if type(retry_seconds) is not int or retry_seconds < 0 or retry_seconds > 31536000:
        return None
    instant = datetime.fromisoformat(timestamp)
    due = instant + timedelta(seconds=max(60 * 2 ** (operation["attempt_count"] - 1), retry_seconds))
    source_id = operation["params"].get("source_id")
    if operation["kind"] == "DISCOVER" and source_id:
        source = connection.execute(select(m.job_sources).where(m.job_sources.c.id == source_id)).mappings().one_or_none()
        if source is None or not source["enabled"]:
            return None
        if source["backoff_until"]:
            due = max(due, datetime.fromisoformat(source["backoff_until"]))
        if source["last_attempt"]:
            due = max(due, datetime.fromisoformat(source["last_attempt"]) + timedelta(seconds=source["poll_interval"]))
    return {
        "next_attempt_at": due.isoformat(),
        "error": code,
        "recovery_metadata": {"action": "RETRY", "reason_code": code, "retryable": True,
                              "retry_after_seconds": min(86400, math.ceil((due - instant).total_seconds()))},
    }
