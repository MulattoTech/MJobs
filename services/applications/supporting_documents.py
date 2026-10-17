"""Explicit profile-owned supporting files selected for one mock application."""
from __future__ import annotations

import re

from sqlalchemy import select

from packages.domain.operation_mode import WorkspaceModeError
from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.documents.uploads import read_resume


_ACTION = "APPLICATION_SUPPORTING_DOCUMENTS_SELECTED"
_FIXTURE_ACTION = "APPLICATION_FIXTURE_SUPPORTING_DOCUMENTS_SELECTED"
_KINDS = frozenset({"SUPPORTING_DOCUMENT", "CERTIFICATION"})
_MAX_DOCUMENTS = 5
_MAX_TOTAL_BYTES = 20 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EDITABLE_STATES = frozenset({"PREPARED", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED"})


class SupportingDocumentError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SupportingDocumentNotFound(SupportingDocumentError):
    pass


class SupportingDocumentConflict(SupportingDocumentError):
    pass


def list_selection(store, profile_id: str, application_id: str, *, _controlled_fixture=False) -> dict:
    if _controlled_fixture:
        _ensure_fixture_workspace(store)
    with store.engine.connect() as connection:
        return _selection_view(connection, profile_id, application_id, _controlled_fixture=_controlled_fixture)


def _selection_view(connection, profile_id: str, application_id: str, *, _controlled_fixture=False) -> dict:
    application = _application(connection, profile_id, application_id)
    revisions, archived = _revisions(connection, profile_id)
    events = _selection_events(connection, application_id, _controlled_fixture=_controlled_fixture)
    return _selection_state(profile_id, application_id, application, revisions, archived,
                            _latest(events))


def _selection_state(profile_id: str, application_id: str, application: dict,
                     revisions: list[dict], archived: set[str], event: dict | None) -> dict:
    selected = _selection_items(event)
    available = [
        _public_revision(row) for row in revisions
        if row["revision_id"] not in archived
    ]
    available_ids = {row["revision_id"] for row in available}
    return {
        "profile_id": profile_id,
        "application_id": application_id,
        "job_id": application["job_id"],
        "available": available,
        "selected_revision_ids": [item["revision_id"] for item in selected],
        "unavailable_revision_ids": [
            item["revision_id"] for item in selected
            if item["revision_id"] not in available_ids
        ],
    }


def select_documents(store, profile_id: str, application_id: str,
                     revision_ids: list[str], request_id: str, *,
                     _controlled_fixture: bool = False) -> dict:
    if _controlled_fixture:
        _ensure_fixture_workspace(store)
    requested = _validate_request(revision_ids, request_id)
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        application = _application(connection, profile_id, application_id, lock=True)
        if _controlled_fixture:
            from services.applications.form_review_providers import normalize_application_route
            route = connection.execute(select(m.routes).where(
                m.routes.c.id == application["route_id"]).with_for_update()).mappings().one()
            if (normalize_application_route(route["provider"], route["url"]) is None
                    or application["state"] not in _EDITABLE_STATES):
                raise SupportingDocumentError("CONTROLLED_FIXTURE_SELECTION_UNSUPPORTED")
        else:
            _ensure_editable(application)
        revisions, archived = _revisions(connection, profile_id)
        by_id = {row["revision_id"]: row for row in revisions}
        selected = []
        total_bytes = 0
        for revision_id in requested:
            row = by_id.get(revision_id)
            if row is None or row["revision_id"] in archived:
                raise SupportingDocumentError("SUPPORTING_DOCUMENT_UNAVAILABLE")
            if row["document_kind"] not in _KINDS:
                raise SupportingDocumentError("SUPPORTING_DOCUMENT_KIND_UNSUPPORTED")
            total_bytes += row["size_bytes"]
            selected.append({
                "revision_id": row["revision_id"],
                "upload_id": row["upload_id"],
                "document_kind": row["document_kind"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
            })
        if total_bytes > _MAX_TOTAL_BYTES:
            raise SupportingDocumentError("SUPPORTING_DOCUMENT_SET_TOO_LARGE")

        events = _selection_events(connection, application_id, _controlled_fixture=_controlled_fixture)
        previous_request = next((
            event for event in events
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("request_id") == request_id
        ), None)
        expected = {
            "profile_id": profile_id,
            "application_id": application_id,
            "job_id": application["job_id"],
            "items": selected,
        }
        if _controlled_fixture:
            expected["context"] = "CONTROLLED_FIXTURE"
        if previous_request is not None:
            saved = dict(previous_request["payload"])
            saved_items = saved.get("items")
            if isinstance(saved_items, list):
                compatible_items = []
                for item in saved_items:
                    if not isinstance(item, dict):
                        compatible_items = None
                        break
                    compatible = dict(item)
                    if "size_bytes" not in compatible:
                        row = by_id.get(compatible.get("revision_id"))
                        if row is not None:
                            compatible["size_bytes"] = row["size_bytes"]
                    compatible_items.append(compatible)
                if compatible_items is not None:
                    saved["items"] = compatible_items
            if any(saved.get(key) != value for key, value in expected.items()):
                raise SupportingDocumentConflict("SUPPORTING_DOCUMENT_REQUEST_ID_REUSED")
            return {**_selection_view(connection, profile_id, application_id, _controlled_fixture=_controlled_fixture),
                    "replayed": True}

        sequence = max((event["payload"].get("sequence", 0) for event in events
                        if isinstance(event.get("payload"), dict)
                        and type(event["payload"].get("sequence", 0)) is int
                        and event["payload"].get("sequence", 0) >= 0), default=0) + 1
        payload = {
            **expected,
            "request_id": request_id,
            "sequence": sequence,
        }
        event_id = identity("application-fixture-supporting-documents" if _controlled_fixture
                            else "application-supporting-documents", application_id, request_id)
        connection.execute(m.audit.insert().values(
            id=event_id,
            entity_id=application_id,
            action=_FIXTURE_ACTION if _controlled_fixture else _ACTION,
            payload=payload,
            timestamp=now(),
        ))

    return {**list_selection(store, profile_id, application_id, _controlled_fixture=_controlled_fixture), "replayed": False}


def read_selected_files(store, output_dir, profile_id: str, application_id: str, *, _controlled_fixture=False) -> list[dict]:
    if _controlled_fixture:
        _ensure_fixture_workspace(store)
    with store.engine.connect() as connection:
        application = _application(connection, profile_id, application_id)
        revisions, archived = _revisions(connection, profile_id)
        by_id = {row["revision_id"]: row for row in revisions}
        event = _latest(_selection_events(connection, application_id, _controlled_fixture=_controlled_fixture))
        state = _selection_state(profile_id, application_id, application, revisions, archived, event)
    if state["unavailable_revision_ids"]:
        raise SupportingDocumentError("SUPPORTING_DOCUMENT_UNAVAILABLE")
    payload = (event or {}).get("payload") or {}
    selected = _selection_items(event)
    if (payload.get("profile_id") != profile_id
            or payload.get("application_id") != application_id
            or payload.get("job_id") != application["job_id"]):
        if selected:
            raise SupportingDocumentError("SUPPORTING_DOCUMENT_SCOPE_MISMATCH")
    result = []
    for snapshot in selected:
        revision_id = snapshot["revision_id"]
        row = by_id.get(revision_id)
        if row is None or revision_id in archived:
            raise SupportingDocumentError("SUPPORTING_DOCUMENT_UNAVAILABLE")
        if row["document_kind"] not in _KINDS:
            raise SupportingDocumentError("SUPPORTING_DOCUMENT_KIND_UNSUPPORTED")
        if (snapshot is None or snapshot.get("upload_id") != row["upload_id"]
                or snapshot.get("document_kind") != row["document_kind"]
                or snapshot.get("sha256") != row["sha256"]):
            raise SupportingDocumentError("SUPPORTING_DOCUMENT_CHANGED")
        expected_size = row["size_bytes"]
        if ("size_bytes" in snapshot
                and (type(snapshot["size_bytes"]) is not int
                     or snapshot["size_bytes"] != expected_size)):
            raise SupportingDocumentError("SUPPORTING_DOCUMENT_CHANGED")
        try:
            record, content = read_resume(
                store, output_dir, profile_id, row["upload_id"],
                document_kind=row["document_kind"],
            )
        except ValueError:
            raise SupportingDocumentError("SUPPORTING_DOCUMENT_CHANGED") from None
        if (record["sha256"] != row["sha256"]
                or record["size_bytes"] != expected_size
                or len(content) != expected_size):
            raise SupportingDocumentError("SUPPORTING_DOCUMENT_CHANGED")
        result.append({
            "revision_id": revision_id,
            "job_id": application["job_id"],
            "filename": record["filename"],
            "media_type": record["media_type"],
            "content": content,
            "sha256": record["sha256"],
        })
    return result


def _application(connection, profile_id: str, application_id: str, *, lock=False) -> dict:
    query = select(m.applications).where(
        m.applications.c.id == application_id,
        m.applications.c.profile_id == profile_id,
    )
    if lock:
        query = query.with_for_update()
    application = connection.execute(query).mappings().one_or_none()
    if application is None:
        raise SupportingDocumentNotFound("APPLICATION_NOT_FOUND")
    return dict(application)


def _revisions(connection, profile_id: str) -> tuple[list[dict], set[str]]:
    rows = [dict(row) for row in connection.execute(select(
        m.resume_revisions.c.id.label("revision_id"),
        m.resume_revisions.c.upload_id,
        m.resume_revisions.c.document_kind,
        m.resume_revisions.c.label,
        m.resume_revisions.c.version,
        m.uploaded_resumes.c.filename,
        m.uploaded_resumes.c.format,
        m.uploaded_resumes.c.size_bytes,
        m.uploaded_resumes.c.sha256,
    ).join(m.uploaded_resumes,
           m.uploaded_resumes.c.id == m.resume_revisions.c.upload_id).where(
        m.resume_revisions.c.profile_id == profile_id,
        m.uploaded_resumes.c.profile_id == profile_id,
        m.resume_revisions.c.document_kind.in_(_KINDS),
    ).order_by(m.resume_revisions.c.created_at.desc(),
               m.resume_revisions.c.version.desc())).mappings()]
    from services.documents.archive_store import list_archive_events
    events = list_archive_events(
        None, profile_id=profile_id, application_id=None, limit=1000,
        connection=connection,
    )
    archived: dict[str, bool] = {}
    for event in events:
        archived[event["document_id"]] = event["action"] == "DOCUMENT_ARCHIVED"
    return rows, {revision_id for revision_id, value in archived.items() if value}


def _selection_events(connection, application_id: str, *, _controlled_fixture=False) -> list[dict]:
    rows = connection.execute(select(m.audit).where(
        m.audit.c.entity_id == application_id,
        m.audit.c.action == (_FIXTURE_ACTION if _controlled_fixture else _ACTION),
    )).mappings()
    return [dict(row) for row in rows]


def _latest(events: list[dict]) -> dict | None:
    def sequence(event):
        payload = event.get("payload")
        value = payload.get("sequence", 0) if isinstance(payload, dict) else 0
        return value if type(value) is int and value >= 0 else 0

    return max(events, key=sequence, default=None)


def _selection_items(event: dict | None) -> list[dict]:
    if event is None:
        return []
    payload = event.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise SupportingDocumentError("SUPPORTING_DOCUMENT_CHANGED")
    items = payload["items"]
    seen = set()
    for item in items:
        if (not isinstance(item, dict)
                or not isinstance(item.get("revision_id"), str)
                or not _ID.fullmatch(item["revision_id"])
                or item["revision_id"] in seen):
            raise SupportingDocumentError("SUPPORTING_DOCUMENT_CHANGED")
        seen.add(item["revision_id"])
    return items


def _public_revision(row: dict) -> dict:
    return {
        "revision_id": row["revision_id"],
        "document_kind": row["document_kind"],
        "label": row["label"],
        "version": row["version"],
        "filename": row["filename"],
        "format": row["format"],
        "size_bytes": row["size_bytes"],
    }


def _validate_request(revision_ids, request_id) -> list[str]:
    if (not isinstance(revision_ids, list) or len(revision_ids) > _MAX_DOCUMENTS
            or any(not isinstance(value, str) or not _ID.fullmatch(value)
                   for value in revision_ids)
            or len(set(revision_ids)) != len(revision_ids)):
        raise SupportingDocumentError("SUPPORTING_DOCUMENT_SELECTION_INVALID")
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise SupportingDocumentError("SUPPORTING_DOCUMENT_REQUEST_INVALID")
    return sorted(revision_ids)


def _ensure_editable(application: dict) -> None:
    if application["target"] != "MOCK":
        raise SupportingDocumentError("EXTERNAL_APPLICATION_DOCUMENTS_DISABLED")
    if application["state"] not in _EDITABLE_STATES:
        raise SupportingDocumentError("APPLICATION_NOT_EDITABLE")


def _ensure_fixture_workspace(store) -> None:
    try:
        store.assert_workspace_mode("FIXTURE")
    except WorkspaceModeError:
        raise SupportingDocumentError("CONTROLLED_FIXTURE_WORKSPACE_REQUIRED") from None
