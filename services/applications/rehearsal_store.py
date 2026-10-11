"""Durable, value-free acknowledgement receipts for local rehearsals."""
from __future__ import annotations

import re

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.applications.rehearsal import build_rehearsal


_ACTION = "APPLICATION_FORM_REHEARSAL_REVIEWED"
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_PROFILE_VERSION = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_VERSION = 1


class RehearsalStoreError(ValueError):
    """A fixed-code error while persisting a rehearsal receipt."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _receipt_id(profile_id: str, application_id: str, request_id: str) -> str:
    return identity("application-form-rehearsal-review-v1", profile_id,
                    application_id, request_id)


def _receipt_payload(profile_id: str, application_id: str, request_id: str,
                     preview: dict) -> dict:
    fingerprint = preview.get("fingerprint")
    profile_version = preview.get("profile_version")
    route_id = preview.get("route_id")
    if (not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint)
            or not isinstance(profile_version, str)
            or not _PROFILE_VERSION.fullmatch(profile_version)
            or not isinstance(route_id, str) or not route_id
            or len(route_id) > 128
            or preview.get("profile_id") != profile_id
            or preview.get("application_id") != application_id
            or preview.get("submission_enabled") is not False):
        raise RehearsalStoreError("APPLICATION_REHEARSAL_INVALID")
    return {
        "receipt_version": _RECEIPT_VERSION,
        "profile_id": profile_id,
        "application_id": application_id,
        "request_id": request_id,
        "fingerprint": fingerprint,
        "profile_version": profile_version,
        "route_id": route_id,
        "acknowledged": True,
    }


def _receipt_view(payload: dict, timestamp: str, *, replayed: bool | None = None) -> dict:
    receipt = {
        "profile_id": payload["profile_id"],
        "application_id": payload["application_id"],
        "request_id": payload["request_id"],
        "fingerprint": payload["fingerprint"],
        "profile_version": payload["profile_version"],
        "reviewed_at": timestamp,
        "acknowledged": True,
    }
    if replayed is not None:
        receipt["replayed"] = replayed
    return receipt


def _latest_review(store, profile_id: str, application_id: str,
                   preview: dict) -> dict | None:
    with store.engine.connect() as connection:
        rows = connection.execute(select(
            m.audit.c.payload, m.audit.c.timestamp,
        ).where(
            m.audit.c.entity_id == application_id,
            m.audit.c.action == _ACTION,
        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc())).mappings()
        for row in rows:
            payload = row["payload"]
            if (not isinstance(payload, dict)
                    or payload.get("profile_id") != profile_id
                    or payload.get("application_id") != application_id
                    or payload.get("acknowledged") is not True
                    or not isinstance(payload.get("request_id"), str)
                    or not _REQUEST_ID.fullmatch(payload["request_id"])
                    or not isinstance(payload.get("fingerprint"), str)
                    or not _FINGERPRINT.fullmatch(payload["fingerprint"])
                    or not isinstance(payload.get("profile_version"), str)
                    or not _PROFILE_VERSION.fullmatch(payload["profile_version"])
                    or not isinstance(payload.get("route_id"), str)
                    or not payload["route_id"]
                    or payload.get("fingerprint") != preview.get("fingerprint")
                    or payload.get("profile_version") != preview.get("profile_version")
                    or payload.get("route_id") != preview.get("route_id")):
                continue
            return _receipt_view(payload, row["timestamp"])
    return None


def get_application_rehearsal(store, profile_id: str, application_id: str) -> dict:
    """Return a fresh value-bearing local preview plus only its latest receipt."""
    preview = build_rehearsal(store, profile_id, application_id)
    if not isinstance(preview, dict):
        raise RehearsalStoreError("APPLICATION_REHEARSAL_INVALID")
    return {
        **preview,
        "latest_review": _latest_review(store, profile_id, application_id, preview),
    }


def acknowledge_application_rehearsal(store, profile_id: str, application_id: str,
                                      request_id: str, fingerprint: str,
                                      acknowledged: bool) -> dict:
    """Persist a sanitized review receipt without copying any rehearsal values."""
    if (not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)
            or not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint)
            or acknowledged is not True):
        raise RehearsalStoreError("APPLICATION_REHEARSAL_INVALID_REQUEST")

    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        application = connection.execute(select(
            m.applications.c.id, m.applications.c.route_id,
        ).where(
            m.applications.c.id == application_id,
            m.applications.c.profile_id == profile_id,
        ).with_for_update()).first()
        if application is None:
            raise RehearsalStoreError("APPLICATION_NOT_FOUND")

        locked_profile = connection.execute(select(
            m.profiles.c.version,
        ).where(
            m.profiles.c.id == profile_id,
        ).with_for_update()).scalar_one_or_none()
        if locked_profile is None:
            raise RehearsalStoreError("PROFILE_NOT_FOUND")
        locked_route = connection.execute(select(
            m.routes.c.id,
        ).where(
            m.routes.c.id == application.route_id,
        ).with_for_update()).scalar_one_or_none()
        if locked_route is None:
            raise RehearsalStoreError("APPLICATION_NOT_FOUND")

        preview = build_rehearsal(store, profile_id, application_id)
        if (preview.get("profile_version") != locked_profile
                or preview.get("route_id") != locked_route):
            raise RehearsalStoreError("APPLICATION_REHEARSAL_STALE")
        expected = _receipt_payload(profile_id, application_id, request_id, preview)
        if expected["fingerprint"] != fingerprint:
            raise RehearsalStoreError("APPLICATION_REHEARSAL_STALE")

        event_id = _receipt_id(profile_id, application_id, request_id)
        prior = connection.execute(select(
            m.audit.c.payload, m.audit.c.timestamp,
        ).where(m.audit.c.id == event_id)).mappings().one_or_none()
        if prior is not None:
            if prior["payload"] != expected:
                raise RehearsalStoreError("APPLICATION_REHEARSAL_REQUEST_ID_REUSED")
            return _receipt_view(prior["payload"], prior["timestamp"], replayed=True)

        timestamp = now()
        connection.execute(m.audit.insert().values(
            id=event_id,
            entity_id=application_id,
            action=_ACTION,
            payload=expected,
            timestamp=timestamp,
        ))
        return _receipt_view(expected, timestamp, replayed=False)
