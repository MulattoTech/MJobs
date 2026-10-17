"""Durable, value-free acknowledgement receipts for local rehearsals."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from sqlalchemy import func, select

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.applications.rehearsal import build_rehearsal


_ACTION = "APPLICATION_FORM_REHEARSAL_REVIEWED"
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_PROFILE_VERSION = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_VERSION = 1
_CONTROL_ACTION = "CONTROLLED_APPLICATION_CONTROLS_CHANGED"
_ADMISSION_ACTION = "CONTROLLED_APPLICATION_ADMITTED"
_SETTING_ACTION = "SETTING_CHANGED"
_CONTROL_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DAILY_LIMIT_MIN = 0
_DAILY_LIMIT_MAX = 100


class RehearsalStoreError(ValueError):
    """A fixed-code error while persisting a rehearsal receipt."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _control_scope(connection, profile_id: str, application_id: str, *, lock=False) -> None:
    statement = select(m.applications.c.id).where(
        m.applications.c.id == application_id,
        m.applications.c.profile_id == profile_id,
    )
    if lock:
        statement = statement.with_for_update()
    if connection.execute(statement).first() is None:
        raise RehearsalStoreError("APPLICATION_NOT_FOUND")
    profile_statement = select(m.profiles.c.id).where(m.profiles.c.id == profile_id)
    if lock:
        profile_statement = profile_statement.with_for_update()
    if connection.execute(profile_statement).first() is None:
        raise RehearsalStoreError("PROFILE_NOT_FOUND")


def _latest_control_payload(connection, application_id: str) -> dict | None:
    rows = list(connection.execute(select(
        m.audit.c.payload, m.audit.c.timestamp, m.audit.c.id,
    ).where(
        m.audit.c.entity_id == application_id,
        m.audit.c.action == _CONTROL_ACTION,
    )).mappings())
    if not rows:
        return None

    # Timestamps can tie at storage precision. Writers serialize on the owner
    # row and increment revision, which gives those events a stable order.
    latest_timestamp = max(row["timestamp"] for row in rows)
    tied = [row for row in rows if row["timestamp"] == latest_timestamp]
    revisions = []
    for row in tied:
        payload = row["payload"]
        if not isinstance(payload, dict):
            raise RehearsalStoreError("CONTROLLED_CONTROLS_INVALID")
        revision = payload.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise RehearsalStoreError("CONTROLLED_CONTROLS_INVALID")
        revisions.append((revision, row["id"], payload))
    payload = max(revisions, key=lambda value: (value[0], value[1]))[2]
    return payload


def _validated_control_payload(payload, profile_id: str, application_id: str) -> dict:
    if (not isinstance(payload, dict)
            or payload.get("profile_id") != profile_id
            or payload.get("application_id") != application_id
            or not isinstance(payload.get("request_id"), str)
            or not _CONTROL_REQUEST_ID.fullmatch(payload["request_id"])
            or type(payload.get("paused")) is not bool
            or type(payload.get("daily_limit")) is not int
            or not _DAILY_LIMIT_MIN <= payload["daily_limit"] <= _DAILY_LIMIT_MAX
            or payload.get("submission_enabled") is not False
            or ("revision" in payload
                and (type(payload["revision"]) is not int or payload["revision"] < 1))
            or set(payload) not in ({"profile_id", "application_id", "request_id", "paused",
                                     "daily_limit", "submission_enabled"},
                                    {"profile_id", "application_id", "request_id", "paused",
                                     "daily_limit", "submission_enabled", "revision"})):
        raise RehearsalStoreError("CONTROLLED_CONTROLS_INVALID")
    return payload


def _controls_from_connection(connection, profile_id: str, application_id: str) -> dict:
    _control_scope(connection, profile_id, application_id)
    payload = _latest_control_payload(connection, application_id)
    if payload is not None:
        return _control_view(_validated_control_payload(payload, profile_id, application_id))
    return {
        "profile_id": profile_id,
        "application_id": application_id,
        "request_id": None,
        "paused": False,
        "daily_limit": 10,
        "submission_enabled": False,
    }


def _control_view(payload: dict) -> dict:
    return {
        "profile_id": payload["profile_id"],
        "application_id": payload["application_id"],
        "request_id": payload["request_id"],
        "paused": payload["paused"],
        "daily_limit": payload["daily_limit"],
        "submission_enabled": False,
    }


def get_controlled_controls(store, profile_id: str, application_id: str) -> dict:
    """Return the latest durable controlled-rehearsal settings, fail closed by default."""
    with store.engine.connect() as connection:
        return _controls_from_connection(connection, profile_id, application_id)


def set_controlled_controls(store, profile_id: str, application_id: str, *,
                            request_id: str, paused: bool, daily_limit: int) -> dict:
    """Persist a value-free, idempotent control change for one owned application."""
    if (not isinstance(request_id, str) or not _CONTROL_REQUEST_ID.fullmatch(request_id)
            or type(paused) is not bool or type(daily_limit) is not int
            or not _DAILY_LIMIT_MIN <= daily_limit <= _DAILY_LIMIT_MAX):
        raise RehearsalStoreError("CONTROLLED_CONTROLS_INVALID_REQUEST")
    event_id = identity("controlled-application-controls-v1", profile_id,
                        application_id, request_id)
    setting_event_id = identity(
        "controlled-application-setting-changed-v1", profile_id,
        application_id, request_id,
    )
    requested = {
        "profile_id": profile_id,
        "application_id": application_id,
        "request_id": request_id,
        "paused": paused,
        "daily_limit": daily_limit,
        "submission_enabled": False,
    }

    def ensure_setting_event(connection, timestamp):
        existing = connection.execute(select(m.audit.c.id).where(
            m.audit.c.id == setting_event_id,
        )).first()
        if existing is None:
            connection.execute(m.audit.insert().values(
                id=setting_event_id, entity_id=profile_id, action=_SETTING_ACTION,
                payload={}, timestamp=timestamp,
            ))

    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        _control_scope(connection, profile_id, application_id, lock=True)
        prior = connection.execute(select(
            m.audit.c.payload,
        ).where(m.audit.c.id == event_id, m.audit.c.action == _CONTROL_ACTION)).scalar_one_or_none()
        if prior is not None:
            if (not isinstance(prior, dict)
                    or any(prior.get(key) != value for key, value in requested.items())
                    or not _validated_control_payload(prior, profile_id, application_id)):
                raise RehearsalStoreError("CONTROLLED_CONTROLS_REQUEST_ID_REUSED")
            ensure_setting_event(connection, now())
            return {"controls": _control_view(prior), "replayed": True}
        current = _latest_control_payload(connection, application_id)
        revision = 1 if current is None else _validated_control_payload(
            current, profile_id, application_id,
        ).get("revision", 0) + 1
        payload = {**requested, "revision": revision}
        timestamp = now()
        connection.execute(m.audit.insert().values(
            id=event_id, entity_id=application_id, action=_CONTROL_ACTION,
            payload=payload, timestamp=timestamp,
        ))
        ensure_setting_event(connection, timestamp)
        return {"controls": _control_view(payload), "replayed": False}


def _require_unpaused(connection, profile_id: str, application_id: str) -> dict:
    controls = _controls_from_connection(connection, profile_id, application_id)
    if controls["paused"]:
        raise RehearsalStoreError("CONTROLLED_REHEARSAL_PAUSED")
    return controls


def require_controlled_admission(connection, profile_id: str, application_id: str) -> None:
    """Enforce pause and UTC-day limits inside the caller's admission transaction."""
    _control_scope(connection, profile_id, application_id, lock=True)
    controls = _require_unpaused(connection, profile_id, application_id)
    utc_now = datetime.now(timezone.utc)
    day_start = utc_now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    admitted_today = connection.execute(select(
        func.count(m.audit.c.id),
    ).where(
        m.audit.c.entity_id == application_id,
        m.audit.c.action == _ADMISSION_ACTION,
        m.audit.c.timestamp >= day_start,
        m.audit.c.payload["profile_id"].as_string() == profile_id,
    )).scalar_one()
    if admitted_today >= controls["daily_limit"]:
        raise RehearsalStoreError("CONTROLLED_DAILY_LIMIT_REACHED")


def _operation_for_owner(connection, profile_id: str, application_id: str,
                         operation_id: str):
    operation = connection.execute(select(
        m.operations.c.id, m.operations.c.kind, m.operations.c.entity_id,
        m.operations.c.params,
    ).where(m.operations.c.id == operation_id).with_for_update()).mappings().one_or_none()
    if (operation is None or operation["kind"] != "APPLICATION_DRY_RUN"
            or operation["entity_id"] != application_id
            or not isinstance(operation["params"], dict)
            or operation["params"].get("profile_id") != profile_id
            or operation["params"].get("application_id") != application_id):
        raise RehearsalStoreError("CONTROLLED_ADMISSION_REQUIRED")
    return operation


def record_controlled_admission(connection, profile_id: str, application_id: str,
                                operation_id: str) -> dict:
    """Record one immutable, metadata-only admission after operation creation."""
    _control_scope(connection, profile_id, application_id, lock=True)
    _operation_for_owner(connection, profile_id, application_id, operation_id)
    event_id = identity("controlled-application-admission-v1", profile_id,
                        application_id, operation_id)
    prior = connection.execute(select(
        m.audit.c.payload, m.audit.c.timestamp,
    ).where(m.audit.c.id == event_id, m.audit.c.action == _ADMISSION_ACTION)
        .with_for_update()).mappings().one_or_none()
    expected = _controlled_admission_payload(profile_id, application_id, operation_id)
    if prior is not None:
        if prior["payload"] != expected:
            raise RehearsalStoreError("CONTROLLED_ADMISSION_CONFLICT")
        return {"operation_id": operation_id, "admitted_at": prior["timestamp"],
                "replayed": True}
    require_controlled_admission(connection, profile_id, application_id)
    admitted_at = now()
    connection.execute(m.audit.insert().values(
        id=event_id, entity_id=application_id, action=_ADMISSION_ACTION,
        payload=expected, timestamp=admitted_at,
    ))
    return {"operation_id": operation_id, "admitted_at": admitted_at, "replayed": False}


def _controlled_admission_payload(profile_id: str, application_id: str,
                                  operation_id: str) -> dict:
    return {
        "profile_id": profile_id,
        "application_id": application_id,
        "operation_id": operation_id,
        "submission_enabled": False,
    }


def require_controlled_execution(connection, profile_id: str, application_id: str,
                                  operation_id: str) -> dict:
    """Require a current, owner-scoped admission and reject execution while paused."""
    _control_scope(connection, profile_id, application_id, lock=True)
    _require_unpaused(connection, profile_id, application_id)
    _operation_for_owner(connection, profile_id, application_id, operation_id)
    event_id = identity("controlled-application-admission-v1", profile_id,
                        application_id, operation_id)
    event = connection.execute(select(
        m.audit.c.payload, m.audit.c.timestamp,
    ).where(m.audit.c.id == event_id, m.audit.c.action == _ADMISSION_ACTION)
        .with_for_update()).mappings().one_or_none()
    expected = _controlled_admission_payload(profile_id, application_id, operation_id)
    if event is None or event["payload"] != expected:
        raise RehearsalStoreError("CONTROLLED_ADMISSION_REQUIRED")
    return {"operation_id": operation_id, "admitted_at": event["timestamp"],
            "submission_enabled": False}


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
        # Consume the cursor before a matching receipt returns early. Otherwise
        # SQLite can retain a read lock past connection return and block the
        # caller's admission transaction from committing.
        rows = list(rows)
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


def get_application_rehearsal(store, profile_id: str, application_id: str, *,
                              preview_builder=None) -> dict:
    """Return a fresh value-bearing local preview plus only its latest receipt."""
    preview = (preview_builder or build_rehearsal)(store, profile_id, application_id)
    if not isinstance(preview, dict):
        raise RehearsalStoreError("APPLICATION_REHEARSAL_INVALID")
    return {
        **preview,
        "latest_review": _latest_review(store, profile_id, application_id, preview),
    }


def acknowledge_application_rehearsal(store, profile_id: str, application_id: str,
                                      request_id: str, fingerprint: str,
                                      acknowledged: bool, *,
                                      preview_builder=None) -> dict:
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

        preview = (preview_builder or build_rehearsal)(store, profile_id, application_id)
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
