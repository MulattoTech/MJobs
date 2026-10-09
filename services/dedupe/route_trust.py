"""Profile-scoped, user-reviewed application-route preferences."""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.dedupe.routes import qualify_route_authority


_ACTION = "APPLICATION_ROUTE_PREFERENCE"


def set_preferred_application_route(store, profile_id: str, company_id: str,
                                    route_id: str, preferred: bool,
                                    request_id: str) -> dict:
    """Record the user's explicit per-company route preference without changing routes."""
    for value, name in ((profile_id, "profile_id"), (company_id, "company_id"),
                        (route_id, "route_id"), (request_id, "request_id")):
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError(f"{name.upper()}_INVALID")
    if type(preferred) is not bool:
        raise ValueError("APPLICATION_ROUTE_PREFERENCE_INVALID")

    with store.engine.begin() as connection:
        if connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == profile_id)).scalar_one_or_none() is None:
            raise LookupError("Profile not found")
        route = connection.execute(select(m.routes).where(
            m.routes.c.id == route_id,
            m.routes.c.company_id == company_id,
        )).mappings().one_or_none()
        if route is None:
            raise LookupError("Application route not found for this company")

        payload = {
            "profile_id": profile_id,
            "company_id": company_id,
            "route_id": route_id,
            "provider": route["provider"],
            "tenant": route["tenant"],
            "authority_at_review": qualify_route_authority(connection, [dict(route)])[0]["authority"],
            "preferred": preferred,
            "request_id": request_id,
        }
        event_id = identity("application-route-preference", profile_id, request_id)
        insert = {"sqlite": sqlite_insert, "postgresql": postgres_insert}.get(
            connection.dialect.name)
        if insert is None:
            raise ValueError("APPLICATION_ROUTE_PERSISTENCE_UNSUPPORTED")
        connection.execute(insert(m.audit).values(
            id=event_id, entity_id=profile_id, action=_ACTION,
            payload=payload, timestamp=now(),
        ).on_conflict_do_nothing(index_elements=[m.audit.c.id]))
        stored = connection.execute(select(
            m.audit.c.payload, m.audit.c.timestamp,
        ).where(m.audit.c.id == event_id,
                m.audit.c.entity_id == profile_id,
                m.audit.c.action == _ACTION)).mappings().one_or_none()
        if stored is None or not isinstance(stored["payload"], Mapping):
            raise ValueError("APPLICATION_ROUTE_PREFERENCE_READBACK_FAILED")
        if dict(stored["payload"]) != payload:
            raise ValueError("APPLICATION_ROUTE_PREFERENCE_CONFLICT")

    return {**payload, "updated_at": stored["timestamp"]}


def preferred_route_identities(store, profile_id: str, company_id: str) -> set[tuple[str, str]]:
    """Read the latest exact provider/tenant choice for one profile and employer."""
    with store.engine.connect() as connection:
        if connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == profile_id)).scalar_one_or_none() is None:
            raise LookupError("Profile not found")
        rows = connection.execute(select(
            m.audit.c.payload, m.audit.c.timestamp, m.audit.c.id,
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action == _ACTION,
        ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings().all()
    latest = {}
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, Mapping) or payload.get("company_id") != company_id:
            continue
        provider, tenant = payload.get("provider"), payload.get("tenant")
        if not isinstance(provider, str) or not isinstance(tenant, str):
            continue
        latest[(provider, tenant)] = payload.get("preferred") is True
    return {key for key, is_preferred in latest.items() if is_preferred}
