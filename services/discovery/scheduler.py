"""Durable source scheduler; it enqueues work and performs no network I/O."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
import time

from sqlalchemy import select

from packages.domain.operation_mode import operation_mode_from_env
from packages.persistence import models as m
from packages.persistence.store import Store
from workers.dispatch import enqueue_operation


ACTIVE_STATES = {"QUEUED", "RUNNING"}


def _parse_timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _jitter(source_id, maximum):
    if maximum <= 0:
        return 0
    digest = hashlib.sha256(source_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (maximum + 1)


def source_due_at(source):
    if not source["last_attempt"]:
        return datetime.min.replace(tzinfo=timezone.utc)
    due = _parse_timestamp(source["last_attempt"]) + timedelta(seconds=source["poll_interval"])
    backoff = _parse_timestamp(source["backoff_until"])
    if backoff and backoff > due:
        due = backoff
    return due + timedelta(seconds=_jitter(source["id"], source["jitter_seconds"]))


def _scheduler_criteria(search_profile):
    criteria = search_profile.get("criteria")
    scheduler = criteria.get("scheduler") if isinstance(criteria, dict) else None
    if scheduler is None:
        return None
    if not isinstance(scheduler, dict) or type(scheduler.get("enabled")) is not bool:
        return None
    if not scheduler["enabled"]:
        return None
    interval = scheduler.get("interval_seconds")
    if type(interval) is not int or not 300 <= interval <= 604800:
        return None
    return interval


def _has_saved_search_scheduler(search_profile):
    criteria = search_profile.get("criteria")
    return isinstance(criteria, dict) and "scheduler" in criteria


def schedule_due_search_profiles(store, at=None):
    """Enqueue due saved-search discovery without performing provider I/O."""
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("Scheduler time must be timezone-aware")

    scheduled = []
    skipped = []
    with store.engine.begin() as connection:
        sources = [dict(row) for row in connection.execute(
            select(m.job_sources).where(m.job_sources.c.enabled.is_(True))).mappings()]
        source_by_id = {row["id"]: row for row in sources}
        profiles = [dict(row) for row in connection.execute(
            select(m.search_profiles).where(m.search_profiles.c.enabled.is_(True))).mappings()]
        receipts = [dict(row) for row in connection.execute(select(m.audit).where(
            m.audit.c.action == "SEARCH_PROFILE_SCHEDULE_ENQUEUED")).mappings()]
        active = list(connection.execute(select(m.operations).where(
            m.operations.c.state.in_(ACTIVE_STATES))).mappings())

        last_enqueued = {}
        for receipt in receipts:
            payload = receipt["payload"]
            if not isinstance(payload, dict) or not isinstance(payload.get("source_id"), str):
                continue
            key = (receipt["entity_id"], payload["source_id"])
            stamp = _parse_timestamp(payload.get("scheduled_for"))
            if stamp and (key not in last_enqueued or stamp > last_enqueued[key]):
                last_enqueued[key] = stamp

        active_sources = {
            row["params"].get("source_id")
            for row in active
            if isinstance(row["params"], dict) and row["params"].get("source_id")
        }
        candidates = []
        for search_profile in profiles:
            interval = _scheduler_criteria(search_profile)
            # Missing, disabled, or malformed scheduler settings cannot create work.
            if interval is None:
                continue
            source_ids = search_profile["source_ids"] or list(source_by_id)
            for source_id in source_ids:
                source = source_by_id.get(source_id)
                if source is None:
                    skipped.append({"source_id": source_id,
                                    "search_profile_id": search_profile["id"],
                                    "reason": "SOURCE_DISABLED_OR_UNKNOWN"})
                    continue
                previous = last_enqueued.get((search_profile["id"], source_id))
                due_at = previous + timedelta(seconds=interval) if previous else datetime.min.replace(tzinfo=timezone.utc)
                if due_at > at:
                    skipped.append({"source_id": source_id,
                                    "search_profile_id": search_profile["id"],
                                    "reason": "PROFILE_NOT_DUE", "due_at": due_at.isoformat()})
                    continue
                source_minimum = source_due_at(source)
                if source_minimum > at:
                    skipped.append({"source_id": source_id,
                                    "search_profile_id": search_profile["id"],
                                    "reason": "SOURCE_NOT_DUE", "due_at": source_minimum.isoformat()})
                    continue
                candidates.append((due_at, search_profile["id"], source_id, search_profile))

        # Preserve source poll_interval as a lower bound and avoid overlapping
        # provider operations. Oldest overdue saved searches get the next slot.
        candidates.sort(key=lambda row: (row[0], row[1], row[2]))
        scheduled_sources = set(active_sources)
        for _, search_id, source_id, search_profile in candidates:
            if source_id in scheduled_sources:
                skipped.append({"source_id": source_id, "search_profile_id": search_id,
                                "reason": "ACTIVE_OPERATION"})
                continue
            scheduled_for = at.isoformat()
            params = {
                "source_id": source_id,
                "profile_ids": [search_profile["profile_id"]],
                "search_profile_id": search_id,
                "scheduled_for": scheduled_for,
                "scheduler": "saved-search-v1",
            }
            operation_id = enqueue_operation(
                store, "DISCOVER", search_profile["profile_id"], params,
                connection=connection,
            )
            store._audit(connection, search_id, "SEARCH_PROFILE_SCHEDULE_ENQUEUED", {
                "profile_id": search_profile["profile_id"],
                "source_id": source_id,
                "scheduled_for": scheduled_for,
            })
            scheduled.append({"source_id": source_id, "search_profile_id": search_id,
                              "operation_id": operation_id, "scheduled_for": scheduled_for})
            scheduled_sources.add(source_id)
            last_enqueued[(search_id, source_id)] = at
    return {"scheduled": scheduled, "skipped": skipped, "checked_at": at.isoformat()}


def schedule_due_sources(store, at=None):
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("Scheduler time must be timezone-aware")
    with store.engine.connect() as connection:
        sources = [dict(row) for row in connection.execute(
            select(m.job_sources).where(m.job_sources.c.enabled.is_(True))).mappings()]
        profiles = [dict(row) for row in connection.execute(
            select(m.search_profiles).where(m.search_profiles.c.enabled.is_(True))).mappings()]
        active = list(connection.execute(select(m.operations).where(
            m.operations.c.state.in_(ACTIVE_STATES))).mappings())
    active_sources = {
        row["params"].get("source_id")
        for row in active
        if isinstance(row["params"], dict) and row["params"].get("source_id")
    }
    scheduled = []
    skipped = []
    for source in sources:
        eligible = [
            row for row in profiles
            if not row["source_ids"] or source["id"] in row["source_ids"]
        ]
        # Saved searches with their own cadence are scheduled independently below.
        profile_ids = sorted({row["profile_id"] for row in eligible
                              if not _has_saved_search_scheduler(row)})
        if not profile_ids:
            skipped.append({"source_id": source["id"], "reason": "NO_SEARCH_PROFILE"})
            continue
        due_at = source_due_at(source)
        if source["id"] in active_sources:
            skipped.append({"source_id": source["id"], "reason": "ACTIVE_OPERATION"})
            continue
        if due_at > at:
            skipped.append({"source_id": source["id"], "reason": "NOT_DUE", "due_at": due_at.isoformat()})
            continue
        schedule_key = "initial" if not source["last_attempt"] else due_at.isoformat()
        params = {
            "source_id": source["id"], "profile_ids": profile_ids,
            "scheduled_for": schedule_key, "scheduler": "source-policy-v1",
        }
        operation_id = enqueue_operation(store, "DISCOVER", profile_ids[0], params)
        scheduled.append({
            "source_id": source["id"], "operation_id": operation_id,
            "profile_ids": profile_ids, "scheduled_for": schedule_key,
        })
    saved_searches = schedule_due_search_profiles(store, at)
    from services.company_discovery.monitoring import schedule_company_sources
    return {"scheduled": scheduled, "skipped": skipped, "checked_at": at.isoformat(),
            "saved_searches": saved_searches,
            "company_sources": schedule_company_sources(store, at)}


def run_forever():
    mode = operation_mode_from_env()
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    interval = int(os.environ.get("MJOBS_SCHEDULER_TICK_SECONDS", "30"))
    if not 5 <= interval <= 300:
        raise ValueError("MJOBS_SCHEDULER_TICK_SECONDS must be 5 through 300")
    store.bind_workspace_mode(mode)
    try:
        while True:
            try:
                schedule_due_sources(store)
            except Exception:
                import logging
                logging.exception("Scheduler tick failed; retrying without losing durable state")
            time.sleep(interval)
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    run_forever()
