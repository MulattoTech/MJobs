"""Privacy-bounded, profile-scoped job visit index (not application tracking)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import identity, upsert

MAX_VIEWS_PER_PROFILE = 100
MAX_AGE_DAYS = 90
DEFAULT_PAGE_SIZE = 30


def _identifier(value: str, name: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or value != value.strip() or value in {".", ".."}
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"RECENT_JOBS_{name}_INVALID")
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_VIEWS_PER_PROFILE:
        raise ValueError("RECENT_JOBS_LIMIT_INVALID")
    return value


def _cutoff(moment: datetime) -> str:
    return (moment - timedelta(days=MAX_AGE_DAYS)).isoformat(timespec="microseconds")


def _lock_profile(connection, profile_id: str) -> None:
    # SQLite must serialize max-order allocation and pruning with other writers.
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif connection.dialect.name != "postgresql":
        raise ValueError("RECENT_JOBS_STORAGE_UNSUPPORTED")
    if connection.execute(
        select(m.profiles.c.id).where(m.profiles.c.id == profile_id).with_for_update()
    ).scalar_one_or_none() is None:
        raise NoResultFound()


def record_job_view(store, profile_id: str, job_id: str) -> dict:
    """Record an explicit open; keep one row per job and at most 100 per profile."""
    profile_id = _identifier(profile_id, "PROFILE")
    job_id = _identifier(job_id, "JOB")
    with store.engine.begin() as connection:
        _lock_profile(connection, profile_id)
        moment = datetime.now(timezone.utc)
        viewed_at = moment.isoformat(timespec="microseconds")
        if connection.execute(
            select(m.jobs.c.id).where(m.jobs.c.id == job_id)
        ).scalar_one_or_none() is None:
            raise NoResultFound()
        connection.execute(delete(m.profile_job_views).where(
            m.profile_job_views.c.profile_id == profile_id,
            m.profile_job_views.c.viewed_at < _cutoff(moment),
        ))
        sequence = connection.execute(select(func.max(m.profile_job_views.c.visit_order)).where(
            m.profile_job_views.c.profile_id == profile_id,
        )).scalar_one() or 0
        if sequence >= 2**63 - 1:
            raise ValueError("RECENT_JOBS_ORDER_EXHAUSTED")
        upsert(connection, m.profile_job_views, {
            "id": identity("recent-job-view", profile_id, job_id),
            "profile_id": profile_id,
            "job_id": job_id,
            "viewed_at": viewed_at,
            "visit_order": sequence + 1,
        }, replace=True)
        old_ids = connection.execute(
            select(m.profile_job_views.c.id).where(
                m.profile_job_views.c.profile_id == profile_id
            ).order_by(m.profile_job_views.c.visit_order.desc(),
                       m.profile_job_views.c.id.desc()).offset(MAX_VIEWS_PER_PROFILE)
        ).scalars().all()
        if old_ids:
            connection.execute(delete(m.profile_job_views).where(m.profile_job_views.c.id.in_(old_ids)))
    return {"profile_id": profile_id, "job_id": job_id, "viewed_at": viewed_at}


def list_recent_job_views(store, profile_id: str, limit: int = DEFAULT_PAGE_SIZE) -> dict:
    """Latest unique visits, including an honest unavailable row for removed jobs."""
    profile_id = _identifier(profile_id, "PROFILE")
    limit = _limit(limit)
    store.profile(profile_id)
    relation = m.profile_job_views.outerjoin(
        m.jobs, m.profile_job_views.c.job_id == m.jobs.c.id
    ).outerjoin(m.companies, m.jobs.c.company_id == m.companies.c.id)
    with store.engine.connect() as connection:
        rows = connection.execute(
            select(m.profile_job_views.c.job_id, m.profile_job_views.c.viewed_at,
                   m.jobs.c.title, m.companies.c.name.label("company"))
            .select_from(relation)
            .where(m.profile_job_views.c.profile_id == profile_id,
                   m.profile_job_views.c.viewed_at >= _cutoff(datetime.now(timezone.utc)))
            .order_by(m.profile_job_views.c.visit_order.desc(), m.profile_job_views.c.id.desc())
            .limit(limit)
        ).mappings().all()
    return {
        "profile_id": profile_id,
        "items": [{
            "job_id": row["job_id"],
            "title": row["title"] if row["title"] is not None else None,
            "company": row["company"] if row["title"] is not None else None,
            "viewed_at": row["viewed_at"],
            "available": row["title"] is not None,
        } for row in rows],
        "max_items": MAX_VIEWS_PER_PROFILE,
        "retention_days": MAX_AGE_DAYS,
    }


def clear_recent_job_views(store, profile_id: str) -> dict:
    """Forget just this profile's visits without touching disposition or audit."""
    profile_id = _identifier(profile_id, "PROFILE")
    with store.engine.begin() as connection:
        _lock_profile(connection, profile_id)
        deleted = connection.execute(
            delete(m.profile_job_views).where(m.profile_job_views.c.profile_id == profile_id)
        ).rowcount
    return {"profile_id": profile_id, "cleared": deleted}


__all__ = ["record_job_view", "list_recent_job_views", "clear_recent_job_views"]
