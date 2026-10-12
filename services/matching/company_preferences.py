"""Profile-scoped, durable company preference decisions."""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.persistence import models as m
from packages.persistence.store import identity, now


COMPANY_PREFERENCE_STATUSES = frozenset({"BLACKLISTED", "PREFERRED", "NEUTRAL"})
_ACTION = "COMPANY_PREFERENCE"


class CompanyPreferenceConflict(ValueError):
    """A request ID was reused with another company preference payload."""


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _append_once(connection, values: dict) -> bool:
    dialect = connection.dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    elif dialect == "postgresql":
        statement = postgres_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    else:
        raise ValueError("Unsupported persistence backend")
    return connection.execute(statement).rowcount == 1


def _profile_company(connection, profile_id: str, company_id: str) -> None:
    if connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id)).scalar_one_or_none() is None:
        raise LookupError("Company preference profile not found")
    if connection.execute(select(m.companies.c.id).where(
            m.companies.c.id == company_id)).scalar_one_or_none() is None:
        raise LookupError("Company preference company not found")


def _discovery_readback(connection, payload):
    discovery = payload.get('discovery')
    if not isinstance(discovery, Mapping):
        return None
    request_id = discovery.get('request_id')
    if request_id:
        request = connection.execute(select(m.company_discovery_requests).where(
            m.company_discovery_requests.c.id == request_id)).mappings().one_or_none()
        if request is not None:
            from services.company_discovery.requests import _status
            return {'request_id': request_id, 'status': _status(connection, request),
                    'reason': 'EMPLOYER_ORIGIN_UNVERIFIED' if request['state'] == 'NEEDS_ORIGIN_REVIEW' else None}
    return dict(discovery)


def _latest_by_company(rows, connection=None) -> dict[str, dict]:
    latest: dict[str, tuple[tuple[str, str], dict]] = {}
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, Mapping):
            continue
        company_id = payload.get("company_id")
        status = payload.get("status")
        request_id = payload.get("request_id")
        if (not isinstance(company_id, str) or not company_id
                or status not in COMPANY_PREFERENCE_STATUSES
                or not isinstance(request_id, str) or not request_id):
            continue
        value = {
            "profile_id": row["entity_id"],
            "company_id": company_id,
            "status": status,
            "updated_at": row["timestamp"],
        }
        if connection is not None:
            discovery = _discovery_readback(connection, payload)
            if discovery is not None:
                value['discovery'] = discovery
        order = (str(row["timestamp"] or ""), str(row["id"]))
        previous = latest.get(company_id)
        if previous is None or order > previous[0]:
            latest[company_id] = (order, value)
    return {company_id: value for company_id, (_, value) in latest.items()}


def get_company_preference(store, profile_id: str, company_id: str) -> dict:
    profile_id = _required(profile_id, "profile_id")
    company_id = _required(company_id, "company_id")
    with store.engine.connect() as connection:
        _profile_company(connection, profile_id, company_id)
        rows = connection.execute(select(
            m.audit.c.id, m.audit.c.entity_id, m.audit.c.payload, m.audit.c.timestamp,
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action == _ACTION,
        ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings().all()
        return _latest_by_company(rows, connection).get(company_id, {
            "profile_id": profile_id,
            "company_id": company_id,
            "status": "NEUTRAL",
            "updated_at": None,
        })


def list_company_preferences(store, profile_id: str) -> list[dict]:
    profile_id = _required(profile_id, "profile_id")
    with store.engine.connect() as connection:
        if connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == profile_id)).scalar_one_or_none() is None:
            raise LookupError("Company preference profile not found")
        rows = connection.execute(select(
            m.audit.c.id, m.audit.c.entity_id, m.audit.c.payload, m.audit.c.timestamp,
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action == _ACTION,
        ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings().all()
        values = _latest_by_company(rows)
        if not values:
            return []
        companies = connection.execute(select(
            m.companies.c.id, m.companies.c.name,
        ).where(m.companies.c.id.in_(values))).mappings().all()
    names = {row["id"]: row["name"] for row in companies}
    return [
        {**values[company_id], "company": names.get(company_id, "")}
        for company_id in sorted(values, key=lambda key: (names.get(key, "").casefold(), key))
    ]


def set_company_preference(store, profile_id: str, company_id: str,
                           status: str, request_id: str, job_id: str | None = None) -> dict:
    profile_id = _required(profile_id, "profile_id")
    company_id = _required(company_id, "company_id")
    request_id = _required(request_id, "request_id")
    if not isinstance(status, str) or status not in COMPANY_PREFERENCE_STATUSES:
        raise ValueError("Unsupported company preference status")

    event_id = identity("company-preference", 1, profile_id, request_id)
    payload = {
        "company_id": company_id,
        "status": status,
        "request_id": request_id,
    }
    if job_id is not None:
        payload['job_id'] = _required(job_id, 'job_id')
    with store.engine.begin() as connection:
        _profile_company(connection, profile_id, company_id)
        profile = store.profile(profile_id, connection=connection)
        inserted = _append_once(connection, {
            "id": event_id,
            "entity_id": profile_id,
            "action": _ACTION,
            "payload": payload,
            "timestamp": now(),
        })
        event = connection.execute(select(
            m.audit.c.payload, m.audit.c.timestamp,
        ).where(
            m.audit.c.id == event_id,
            m.audit.c.entity_id == profile_id,
            m.audit.c.action == _ACTION,
        )).mappings().one_or_none()
        if event is None or not isinstance(event["payload"], Mapping):
            raise CompanyPreferenceConflict("Company preference event could not be confirmed")
        stored = dict(event['payload'])
        if {key: value for key, value in stored.items() if key != 'discovery'} != payload:
            raise CompanyPreferenceConflict("Request ID was already used for another company preference")
        if inserted and job_id is not None:
            job_company = connection.execute(select(m.jobs.c.company_id).where(
                m.jobs.c.id == job_id)).scalar_one_or_none()
            # Public captured jobs may legitimately be unscored. Explicit user
            # intent needs employer provenance, not automatic match eligibility.
            if job_company != company_id:
                raise ValueError('Company preference job context is invalid')
        if inserted and job_id is not None and status == 'PREFERRED':
            from services.company_discovery.requests import record_discovery_request
            discovery_id = record_discovery_request(connection, job_id, profile,
                explicit_reason='USER_PREFERRED_COMPANY')
            stored['discovery'] = {'request_id': discovery_id, 'status': 'NOT_QUEUED',
                'reason': 'POLICY_OR_SOURCE_NOT_ELIGIBLE' if discovery_id is None else None}
            connection.execute(m.audit.update().where(m.audit.c.id == event_id).values(payload=stored))
        discovery = _discovery_readback(connection, stored)
    return {
        "profile_id": profile_id,
        "company_id": company_id,
        "status": status,
        "updated_at": event["timestamp"],
        "replayed": not inserted,
        **({'discovery': discovery} if discovery is not None else {}),
    }
