"""Project fixture-only direct monitoring into the company-source view."""
import os
import re
from urllib.parse import urlsplit

from sqlalchemy import select

from packages.persistence import models as m
from services.company_discovery.fixture_monitoring import MARKER, read_fixture_monitoring


_MONITORING_FIELDS = (
    "revision", "enabled", "poll_interval", "status", "failure_count",
    "last_attempt", "next_due", "last_error", "operation_id",
    "completed_operation_id",
)


def fixture_company_monitoring_view(store, origin, profile_id, row):
    """Return a view overlay only for an associated local direct fixture source."""
    if (not profile_id or row.get("profile_id") != profile_id
            or row.get("profile_company_associated") is not True):
        return None
    with store.engine.connect() as connection:
        source = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == row.get("id"))).mappings().one_or_none()
        if source is None:
            return None
        source = dict(source)
        monitor_row = connection.execute(select(m.company_source_monitoring).where(
            m.company_source_monitoring.c.source_id == source["id"])).mappings().one_or_none()
        monitor_row = dict(monitor_row) if monitor_row is not None else None

    try:
        parsed_origin = urlsplit(origin)
        parsed_career = urlsplit(source["career_url"] or "")
    except (TypeError, ValueError):
        return None
    local_hosts = {"localhost", "127.0.0.1"} | (
        {"mock"} if os.environ.get("MJOBS_COMPOSE_MOCK") == "1" else set()
    )
    tenant = source["tenant"]
    if (parsed_origin.scheme != "http" or parsed_origin.hostname not in local_hosts
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tenant or "")
            or source["provider"] != "mock-ats" or source["collection_strategy"] != "FIXTURE"
            or source["collector_source_id"] is not None
            or source["endpoint"] != origin.rstrip("/") + "/ats/" + tenant + "/jobs"
            or (parsed_career.scheme, parsed_career.netloc) != (parsed_origin.scheme, parsed_origin.netloc)):
        return None

    if monitor_row is None:
        return {"monitoring_supported": True, "monitoring": None, "next_check": None}

    binding = (monitor_row.get("source_snapshot") or {}).get("fixture", {})
    if not isinstance(binding, dict) or binding.get("marker") != MARKER:
        state = {**monitor_row, "status": "BLOCKED",
                 "last_error": "FIXTURE_MONITOR_REVIEW_REQUIRED", "next_due": None}
    elif binding.get("profile_id") != profile_id:
        state = {**monitor_row, "status": "BLOCKED",
                 "last_error": "FIXTURE_MONITOR_PROFILE_SOURCE_MISMATCH", "next_due": None}
    else:
        try:
            state = read_fixture_monitoring(store, origin, profile_id, source["id"])
        except ValueError as error:
            # Keep stale or invalid fixture reviews visible as blocked with their
            # direct monitor error; do not inherit the generic PERSONAL validator's
            # unsupported-source reason.
            state = {**monitor_row, "status": "BLOCKED", "last_error": str(error), "next_due": None}
    monitoring = {key: state.get(key) for key in _MONITORING_FIELDS}
    monitoring["rediscovery_operation_id"] = None
    return {
        "monitoring_supported": True,
        "monitoring": monitoring,
        "next_check": state.get("next_due"),
    }
