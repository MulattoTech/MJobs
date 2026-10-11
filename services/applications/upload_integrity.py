"""Hash-verified immutable upload buffers and bounded browser File readback.

Proof is limited to the selected browser File objects. It is not an assertion
that an external ATS accepted, stored, or submitted these documents.
"""
from dataclasses import dataclass
import base64
import binascii
import hashlib
import re

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now

ACTION = "APPLICATION_BROWSER_UPLOADS_VERIFIED_V1"
MAX_PREPARED_BYTES = 2_000_000  # Existing controlled mock provider contract.
MAX_SUPPORTING_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
_SHA = re.compile(r"^[a-f0-9]{64}$")
_KINDS = {"resume", "cover-letter", "supporting-document"}


class UploadIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class UploadSnapshot:
    kind: str
    document_id: str
    filename: str
    media_type: str
    content: bytes
    sha256: str

    def manifest(self):
        return {"kind": self.kind, "document_id": self.document_id,
                "size_bytes": len(self.content), "sha256": self.sha256}


def snapshot_bytes(kind, document_id, filename, media_type, content, expected_sha256):
    if kind not in _KINDS:
        raise UploadIntegrityError("UPLOAD_KIND_UNSUPPORTED")
    if (not isinstance(filename, str) or not filename or len(filename) > 255
            or any(char in filename for char in ("/", "\\"))
            or any(ord(char) < 32 for char in filename)):
        raise UploadIntegrityError("UPLOAD_FILENAME_INVALID")
    if not isinstance(document_id, str) or not document_id or len(document_id) > 128:
        raise UploadIntegrityError("UPLOAD_DOCUMENT_ID_INVALID")
    maximum = MAX_SUPPORTING_BYTES if kind == "supporting-document" else MAX_PREPARED_BYTES
    if not isinstance(content, bytes) or not 0 < len(content) <= maximum:
        raise UploadIntegrityError("UPLOAD_SIZE_INVALID")
    if (not isinstance(expected_sha256, str) or not _SHA.fullmatch(expected_sha256)
            or hashlib.sha256(content).hexdigest() != expected_sha256):
        raise UploadIntegrityError("UPLOAD_CONTENT_CHANGED")
    if kind in {"resume", "cover-letter"} and (
            media_type != "application/pdf" or not content.startswith(b"%PDF-")):
        raise UploadIntegrityError("UPLOAD_PREPARED_PDF_INVALID")
    if media_type not in {"application/pdf", "text/plain",
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}:
        raise UploadIntegrityError("UPLOAD_MEDIA_TYPE_INVALID")
    return UploadSnapshot(kind, document_id, filename, media_type, content, expected_sha256)


def snapshot_prepared(document, path):
    try:
        with path.open("rb") as source:
            content = source.read(MAX_PREPARED_BYTES + 1)
    except OSError:
        raise UploadIntegrityError("UPLOAD_FILE_UNAVAILABLE") from None
    return snapshot_bytes(document["kind"], document["id"], path.name,
                          "application/pdf", content, document["sha256"]["pdf"])


def snapshot_supporting(document):
    return snapshot_bytes("supporting-document", document["revision_id"],
                          document["filename"], document["media_type"],
                          document["content"], document["sha256"])


def _bounded(snapshots):
    if (not isinstance(snapshots, (list, tuple)) or not 0 < len(snapshots) <= 5
            or not all(isinstance(item, UploadSnapshot) for item in snapshots)
            or sum(len(item.content) for item in snapshots) > MAX_TOTAL_BYTES):
        raise UploadIntegrityError("UPLOAD_SET_INVALID")


def verify_file_selection(locator, snapshots, *, check=lambda: None):
    """Read selected bytes back in Chromium and compare exact count/name/hash."""
    _bounded(snapshots)
    check()
    actual = locator.evaluate("""async input => {
      if (input.tagName !== 'INPUT' || input.type !== 'file') return null;
      const files = Array.from(input.files || []);
      if (!files.length || files.length > 5 ||
          files.some(f => !f.size || f.size > 10485760) ||
          files.reduce((sum,f) => sum + f.size, 0) > 25165824) return null;
      let timer;
      try {
        return await Promise.race([
          Promise.all(files.map(async file => ({
            name: file.name, size: file.size, type: file.type,
            content: await new Promise((resolve, reject) => {
              const reader = new FileReader();
              reader.onload = () => resolve(String(reader.result).split(',')[1]);
              reader.onerror = () => reject(new Error('UPLOAD_FILE_READ_FAILED'));
              reader.readAsDataURL(file);
            })
          }))),
          new Promise(resolve => {timer = setTimeout(() => resolve(null), 5000);})
        ]);
      } finally {clearTimeout(timer);}
    }""")
    check()
    # Container HTTP names are not secure browser contexts, so Web Crypto is
    # unavailable there. Hash the bounded File readback locally instead. Raw
    # bytes are transient and never enter the audit manifest or diagnostics.
    if not isinstance(actual, list) or len(actual) != len(snapshots):
        raise UploadIntegrityError("UPLOAD_BROWSER_BYTES_MISMATCH")
    verified = []
    for row, snapshot in zip(actual, snapshots):
        if (not isinstance(row, dict) or set(row) != {"name", "size", "type", "content"}
                or type(row["size"]) is not int or row["size"] != len(snapshot.content)
                or not isinstance(row["content"], str)
                or len(row["content"]) != 4 * ((len(snapshot.content) + 2) // 3)):
            raise UploadIntegrityError("UPLOAD_BROWSER_BYTES_MISMATCH")
        try:
            content = base64.b64decode(row["content"], validate=True)
        except (ValueError, binascii.Error):
            raise UploadIntegrityError("UPLOAD_BROWSER_BYTES_MISMATCH") from None
        if len(content) != len(snapshot.content):
            raise UploadIntegrityError("UPLOAD_BROWSER_BYTES_MISMATCH")
        verified.append({"name": row["name"], "size": row["size"],
                         "type": row["type"], "sha256": hashlib.sha256(content).hexdigest()})
    expected = [{"name": item.filename, "size": len(item.content),
                 "type": item.media_type, "sha256": item.sha256} for item in snapshots]
    if verified != expected:
        raise UploadIntegrityError("UPLOAD_BROWSER_BYTES_MISMATCH")
    return [item.manifest() for item in snapshots]


def attach_verified_files(locator, snapshots, *, check=lambda: None):
    _bounded(snapshots)
    check()
    locator.set_input_files([{"name": item.filename, "mimeType": item.media_type,
                              "buffer": item.content} for item in snapshots])
    return verify_file_selection(locator, snapshots, check=check)


def record_verified_uploads(store, application_id, attempt_id, manifest):
    """Persist only document identities, sizes and digests; no paths or values."""
    if (not isinstance(manifest, list) or not 2 <= len(manifest) <= 7
            or any(not isinstance(item, dict) or set(item) != {
                "kind", "document_id", "size_bytes", "sha256"}
                or item["kind"] not in _KINDS
                or not isinstance(item["document_id"], str)
                or not isinstance(item["sha256"], str) or not _SHA.fullmatch(item["sha256"])
                or type(item["size_bytes"]) is not int or item["size_bytes"] <= 0
                for item in manifest)):
        raise UploadIntegrityError("UPLOAD_MANIFEST_INVALID")
    with store.engine.begin() as connection:
        attempt = connection.execute(select(m.attempts).where(
            m.attempts.c.id == attempt_id,
            m.attempts.c.application_id == application_id).with_for_update()).mappings().one_or_none()
        if attempt is None or attempt["state"] != "RUNNING":
            raise UploadIntegrityError("UPLOAD_ATTEMPT_NOT_ACTIVE")
        event_id = identity(ACTION, attempt_id)
        if connection.execute(select(m.audit.c.id).where(m.audit.c.id == event_id)).first():
            raise UploadIntegrityError("UPLOAD_MANIFEST_ALREADY_RECORDED")
        connection.execute(m.audit.insert().values(
            id=event_id, entity_id=application_id, action=ACTION, timestamp=now(),
            payload={"attempt_id": attempt_id, "verification": "BROWSER_SELECTED_BYTES",
                     "documents": manifest, "provider_acceptance_claimed": False}))
