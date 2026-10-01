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
        profile_ids = sorted({row["profile_id"] for row in eligible})
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
    return {"scheduled": scheduled, "skipped": skipped, "checked_at": at.isoformat()}


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
