"""Archive immutable generated and uploaded document revisions by audit state."""
from __future__ import annotations

from sqlalchemy import select

from packages.persistence import models as m
from services.documents.archive_store import append_archive_event, list_archive_events


_ARCHIVE = "DOCUMENT_ARCHIVED"
_RESTORE = "DOCUMENT_ARCHIVE_RESTORED"


def archive_document(store, *, profile_id: str, document_id: str,
                     operation_id: str) -> dict:
    """Hide one immutable revision from the active library without deleting it."""
    return _set_archived(store, profile_id, document_id, operation_id, archived=True)


def restore_document(store, *, profile_id: str, document_id: str,
                     operation_id: str) -> dict:
    """Restore one previously archived revision, retaining its original history."""
    return _set_archived(store, profile_id, document_id, operation_id, archived=False)


def document_archive_state(store, *, profile_id: str,
                           application_id: str | None = None) -> dict[str, bool]:
    """Return latest archive state for revisions in one exact ownership scope.

    Missing IDs mean active. Generated document IDs are application-scoped;
    uploaded resume revision IDs are profile-scoped and use ``application_id=None``.
    """
    store.profile(profile_id)
    events = list_archive_events(store, profile_id=profile_id,
                                 application_id=application_id, limit=1000)
    return _project_state(events)


def _set_archived(store, profile_id: str, document_id: str,
                  operation_id: str, *, archived: bool) -> dict:
    _validate_identifier("profile_id", profile_id)
    _validate_identifier("document_id", document_id)
    _validate_identifier("operation_id", operation_id)
    store.profile(profile_id)
    action = _ARCHIVE if archived else _RESTORE

    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        revision = _owned_revision(connection, profile_id, document_id)
        application_id = revision["application_id"]
        revision_payload = revision["archive_payload"]
        events = list_archive_events(
            store, profile_id=profile_id, application_id=application_id,
            document_id=document_id, limit=1000, connection=connection,
        )
        prior = _project_state(events).get(document_id, False)
        previous_operation = next((event for event in events
                                   if event["action"] == action
                                   and event["operation_id"] == operation_id), None)
        if previous_operation is not None:
            if previous_operation["payload"] != {
                    **revision_payload, "profile_id": profile_id,
                    "application_id": application_id, "document_id": document_id,
                    "operation_id": operation_id}:
                raise ValueError("Archive operation identity was reused for different revision data")

        event = previous_operation
        changed = False
        if prior != archived and previous_operation is None:
            before_ids = {item["id"] for item in events}
            event = append_archive_event(
                store, profile_id=profile_id, application_id=application_id,
                document_id=document_id, action=action, operation_id=operation_id,
                payload=revision_payload, connection=connection,
            )
            changed = event["id"] not in before_ids
            if changed:
                events.append(event)
        current = _project_state(events).get(document_id, False)
        return {"profile_id": profile_id, "application_id": application_id,
                "document_id": document_id, "archived": current,
                "changed": changed, "event": event}


def _owned_revision(connection, profile_id: str, document_id: str) -> dict:
    generated = connection.execute(select(
        m.documents, m.applications.c.profile_id,
    ).join(m.applications, m.applications.c.id == m.documents.c.application_id).where(
        m.documents.c.id == document_id,
        m.applications.c.profile_id == profile_id,
    ).with_for_update()).mappings().one_or_none()
    if generated is not None:
        return {
            "application_id": generated["application_id"],
            "archive_payload": {
                "revision_type": "GENERATED",
                "kind": generated["kind"],
                "version": generated["version"],
                "profile_version": generated["profile_version"],
                "sha256": dict(generated["sha256"] or {}),
            },
        }

    uploaded = connection.execute(select(
        m.resume_revisions.c.id, m.resume_revisions.c.document_kind,
        m.resume_revisions.c.version, m.resume_revisions.c.series_id,
        m.resume_revisions.c.profile_id, m.uploaded_resumes.c.sha256,
    ).join(m.uploaded_resumes,
           m.uploaded_resumes.c.id == m.resume_revisions.c.upload_id).where(
        m.resume_revisions.c.id == document_id,
        m.resume_revisions.c.profile_id == profile_id,
        m.uploaded_resumes.c.profile_id == profile_id,
    ).with_for_update()).mappings().one_or_none()
    if uploaded is None:
        raise LookupError("Document is not available to this profile")
    return {
        "application_id": None,
        "archive_payload": {
            "revision_type": "UPLOADED",
            "kind": uploaded["document_kind"],
            "version": uploaded["version"],
            "series_id": uploaded["series_id"],
            "sha256": uploaded["sha256"],
        },
    }


def _project_state(events: list[dict]) -> dict[str, bool]:
    state: dict[str, bool] = {}
    for event in events:
        state[event["document_id"]] = event["action"] == _ARCHIVE
    return state


def _validate_identifier(name: str, value: str) -> None:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 128):
        raise ValueError(f"{name} must be a non-empty identifier of at most 128 characters")
