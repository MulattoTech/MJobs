"""Pure, review-first validation for synthetic Workday upload selections.

Recovered from preserved #684 (``ao/mjobs-37/workday-upload-integrity``),
commit ``5e85320b967a5351d91c5dabb395b82eea42439b``. This preflight consumes
immutable snapshots created by the shared upload-integrity helpers. It does
not inspect employer pages, attach files, or submit applications.
"""
from collections.abc import Mapping

from services.applications.upload_integrity import UploadSnapshot, snapshot_bytes


_SUPPORTED_KINDS = ("resume", "cover-letter")


def preflight_workday_uploads(file_fields, snapshots):
    """Validate supported Workday file slots against immutable snapshots.

    A resume snapshot is required when a recognized resume slot exists. A
    cover letter is required only when its recognized slot is required. Every
    supplied snapshot must match exactly one unambiguous supported slot. The
    result contains digest-only manifests, never document values or bytes.
    """
    if not isinstance(file_fields, (list, tuple)) or len(file_fields) > 2:
        raise ValueError("WORKDAY_UPLOAD_FIELDS_INVALID")
    fields = {}
    for field in file_fields:
        if (not isinstance(field, Mapping)
                or set(field) != {"kind", "required", "ambiguous"}
                or field.get("kind") not in _SUPPORTED_KINDS
                or type(field.get("required")) is not bool
                or field.get("ambiguous") is not False
                or field["kind"] in fields):
            raise ValueError("WORKDAY_UPLOAD_FIELDS_INVALID")
        fields[field["kind"]] = field

    if not isinstance(snapshots, (list, tuple)) or len(snapshots) > 2:
        raise ValueError("WORKDAY_UPLOAD_SET_INVALID")
    by_kind = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, UploadSnapshot) or snapshot.kind not in _SUPPORTED_KINDS:
            raise ValueError("WORKDAY_UPLOAD_SET_INVALID")
        if snapshot.kind in by_kind or snapshot.kind not in fields:
            raise ValueError("WORKDAY_UPLOAD_SET_INVALID")
        try:
            verified = snapshot_bytes(
                snapshot.kind, snapshot.document_id, snapshot.filename,
                snapshot.media_type, snapshot.content, snapshot.sha256,
            )
        except (TypeError, ValueError):
            raise ValueError("WORKDAY_UPLOAD_SNAPSHOT_INVALID") from None
        by_kind[snapshot.kind] = verified

    if "resume" in fields and "resume" not in by_kind:
        raise ValueError("WORKDAY_UPLOAD_REQUIRED_MISSING")
    if (fields.get("cover-letter", {}).get("required")
            and "cover-letter" not in by_kind):
        raise ValueError("WORKDAY_UPLOAD_REQUIRED_MISSING")

    return [by_kind[kind].manifest() for kind in _SUPPORTED_KINDS if kind in by_kind]
