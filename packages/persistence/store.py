"""Transactional store with immutable fact versions and deterministic identity."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.domain.safety import Fact
from packages.persistence import models as m


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def upsert(connection, table, data, replace=False):
    insert = {"postgresql": postgres_insert, "sqlite": sqlite_insert}.get(connection.dialect.name)
    if insert is None:
        raise ValueError("Unsupported persistence backend")
    statement = insert(table).values(**data)
    statement = statement.on_conflict_do_update(index_elements=[table.c.id], set_=data) if replace else statement.on_conflict_do_nothing(index_elements=[table.c.id])
    connection.execute(statement)


class Store:
    def __init__(self, url):
        self.url = url
        options = {"connect_args": {"check_same_thread": False, "timeout": 15}} if url.startswith("sqlite:") else {"connect_args": {"connect_timeout": 10}}
        self.engine = create_engine(url, pool_pre_ping=True, **options)
        if url.startswith("sqlite:"):
            @event.listens_for(self.engine, "connect")
            def foreign_keys(dbapi_connection, _):
                dbapi_connection.execute("PRAGMA foreign_keys=ON")

    def migrate(self):
        """Test-only schema setup; deployment uses versioned Alembic migrations."""
        if not self.url.startswith("sqlite:"):
            raise ValueError("Use alembic upgrade head for PostgreSQL")
        m.metadata.create_all(self.engine)

    def save_profile(self, profile):
        if not all(isinstance(profile.get(field), str) and profile[field].strip() for field in ("id", "name", "email")):
            raise ValueError("Profile identity/contact fields are required")
        if not profile.get("facts") or len(profile["facts"]) > 200:
            raise ValueError("Provide 1 to 200 explicit career facts")
        verified = [Fact(**{key: row[key] for key in ("id", "text", "source", "verified", "user_approved", "confidence")}) for row in profile["facts"]]
        if len({fact.id for fact in verified}) != len(verified):
            raise ValueError("Duplicate fact identity")
        version = identity(profile)
        with self.engine.begin() as connection:
            upsert(connection, m.profiles, {"id": profile["id"], "name": profile["name"], "email": profile["email"], "version": version}, replace=True)
            answers = profile.get("answers", [])
            if len({answer["id"] for answer in answers}) != len(answers):
                raise ValueError("Duplicate approved answer identity")
            for answer in answers:
                Fact(**answer)
            upsert(connection, m.profile_versions, {"id": identity(profile["id"], version), "profile_id": profile["id"], "version": version,
                   "contact": {"name": profile["name"], "email": profile["email"]}, "answers": answers})
            for row, fact in zip(profile["facts"], verified):
                data = asdict(fact)
                data.update(id=identity(profile["id"], version, fact.id), fact_id=fact.id,
                            kind=row.get("kind", "skill"), profile_id=profile["id"], version=version)
                upsert(connection, m.facts, data)
            self._audit(connection, profile["id"], "PROFILE_SAVED", {"version": version})
        return version

    def profile(self, profile_id, version=None):
        with self.engine.connect() as connection:
            row = connection.execute(select(m.profiles).where(m.profiles.c.id == profile_id)).mappings().one()
            data = dict(row)
            data["version"] = version or data["version"]
            snapshot = connection.execute(select(m.profile_versions).where(m.profile_versions.c.id == identity(profile_id, data["version"]))).mappings().one()
            data.update(snapshot["contact"])
            data["answers"] = snapshot["answers"]
            data["facts"] = [dict(fact) for fact in connection.execute(select(m.facts).where(m.facts.c.profile_id == profile_id, m.facts.c.version == data["version"])).mappings()]
            return data

    def profiles(self):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.profiles).order_by(m.profiles.c.name)).mappings()]

    def jobs(self, profile_id=None, q="", remote_type=None, provider=None, sort="score", limit=200):
        profile = self.profile(profile_id) if profile_id else None
        with self.engine.connect() as connection:
            counts = select(m.postings.c.job_id, func.count().label("posting_count")).group_by(m.postings.c.job_id).subquery()
            statement = select(m.jobs, m.companies.c.name.label("company"), m.routes.c.provider, m.routes.c.url.label("application_url"),
                counts.c.posting_count, m.matches.c.score.label("match_score"), m.matches.c.components.label("match_components"), m.matches.c.missing,
                m.applications.c.state.label("application_state"), m.applications.c.id.label("application_id")).select_from(
                m.jobs.join(m.companies, m.jobs.c.company_id == m.companies.c.id).join(m.routes, m.routes.c.job_id == m.jobs.c.id)
                .outerjoin(counts, counts.c.job_id == m.jobs.c.id)
                .outerjoin(m.matches, (m.matches.c.job_id == m.jobs.c.id) & (m.matches.c.profile_id == profile_id) &
                           (m.matches.c.profile_version == (profile["version"] if profile else "")))
                .outerjoin(m.applications, (m.applications.c.job_id == m.jobs.c.id) & (m.applications.c.profile_id == profile_id)))
            if q:
                statement = statement.where(m.jobs.c.title.icontains(q, autoescape=True) | m.companies.c.name.icontains(q, autoescape=True) | m.jobs.c.description.icontains(q, autoescape=True))
            if remote_type:
                statement = statement.where(m.jobs.c.remote_type == remote_type)
            if provider:
                statement = statement.where(m.routes.c.provider == provider)
            statement = statement.order_by(m.matches.c.score.desc().nulls_last(), m.jobs.c.title, m.jobs.c.id) if sort == "score" else statement.order_by(m.jobs.c.title, m.jobs.c.id)
            return [dict(row) for row in connection.execute(statement.limit(limit)).mappings()]

    def job(self, job_id, profile_id=None):
        profile = self.profile(profile_id) if profile_id else None
        with self.engine.connect() as connection:
            data = dict(connection.execute(select(m.jobs, m.companies.c.name.label("company"), m.routes.c.provider, m.routes.c.url.label("application_url"))
                .join(m.companies, m.jobs.c.company_id == m.companies.c.id).join(m.routes, m.routes.c.job_id == m.jobs.c.id).where(m.jobs.c.id == job_id)).mappings().one())
            match = connection.execute(select(m.matches).where(m.matches.c.job_id == job_id, m.matches.c.profile_id == profile_id,
                m.matches.c.profile_version == (profile["version"] if profile else ""))).mappings().one_or_none()
            data["match"] = dict(match) if match else None
            data["postings"] = []
            for row in connection.execute(select(m.postings).where(m.postings.c.job_id == job_id)).mappings():
                posting = dict(row)
                posting["snapshots"] = [dict(snapshot) for snapshot in connection.execute(select(m.posting_snapshots.c.id, m.posting_snapshots.c.sha256,
                    m.posting_snapshots.c.retrieved_at).where(m.posting_snapshots.c.posting_id == row["id"])).mappings()]
                data["postings"].append(posting)
            return data

    def applications(self, profile_id=None):
        with self.engine.connect() as connection:
            statement = select(m.applications, m.jobs.c.title, m.companies.c.name.label("company"), m.routes.c.url.label("application_url")).join(m.jobs,
                m.jobs.c.id == m.applications.c.job_id).join(m.companies, m.companies.c.id == m.jobs.c.company_id).join(m.routes, m.routes.c.id == m.applications.c.route_id)
            if profile_id:
                statement = statement.where(m.applications.c.profile_id == profile_id)
            return [dict(row) for row in connection.execute(statement.order_by(m.applications.c.created_at.desc()).limit(200)).mappings()]

    def count(self, name):
        table = m.metadata.tables[name]
        with self.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(table)).scalar_one()

    def application(self, application_id):
        with self.engine.connect() as connection:
            return dict(connection.execute(select(m.applications).where(m.applications.c.id == application_id)).mappings().one())

    def transition(self, application_id, expected, state, step=None, confirmation=None):
        with self.engine.begin() as connection:
            row = connection.execute(select(m.applications).where(m.applications.c.id == application_id).with_for_update()).mappings().one()
            if row["state"] not in expected:
                raise ValueError(f"Unsafe application transition from {row['state']}")
            trace = [*row["trace"], *([step] if step else [])]
            values = {"state": state, "trace": trace}
            if confirmation is not None:
                values["confirmation"] = confirmation
            changed = connection.execute(update(m.applications).where(m.applications.c.id == application_id, m.applications.c.state == row["state"]).values(**values))
            if changed.rowcount != 1:
                raise ValueError("Application state changed concurrently")
            self._audit(connection, application_id, state, {"step": step})

    def _audit(self, connection, entity, action, payload):
        connection.execute(m.audit.insert().values(id=uuid4().hex, entity_id=entity, action=action, payload=payload, timestamp=now()))
