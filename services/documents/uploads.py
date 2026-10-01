"""Store unreviewed base resumes locally; never turn file contents into facts."""
from __future__ import annotations

from io import BytesIO
import hashlib
import os
from pathlib import Path
import tempfile
import unicodedata
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
}


class UploadIntegrityError(ValueError):
    """A stored file no longer matches its durable metadata."""


def validate_resume(filename: str, media_type: str, data: bytes) -> str:
    if not isinstance(filename, str) or not filename or len(filename) > 200 or filename != filename.strip():
        raise ValueError("Resume filename must be 1 to 200 characters without surrounding space")
    if unicodedata.normalize("NFC", filename) != filename or any(
            character in "/\\:" or unicodedata.category(character).startswith("C") for character in filename):
        raise ValueError("Resume filename contains an unsafe character")
    if filename.endswith(".") or filename in (".", ".."):
        raise ValueError("Resume filename is unsafe")
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension not in MEDIA_TYPES or media_type != MEDIA_TYPES[extension]:
        raise ValueError("Resume extension and MIME type must be matching PDF, DOCX or UTF-8 TXT")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Resume must be nonempty and no larger than 10 MiB")
    if extension == "pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
            raise ValueError("PDF signature or end marker is invalid")
    elif extension == "docx":
        try:
            with ZipFile(BytesIO(data)) as archive:
                members = archive.infolist()
                names = {member.filename for member in members}
                if len(members) > 1000 or sum(member.file_size for member in members) > 30 * MAX_UPLOAD_BYTES:
                    raise ValueError("DOCX archive exceeds safe inventory limits")
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise ValueError("DOCX document parts are missing")
                if any(member.flag_bits & 1 or member.filename.startswith("/") or "\\" in member.filename or
                       any(part in (".", "..") for part in member.filename.split("/")) for member in members):
                    raise ValueError("DOCX archive contains unsafe entries")
                content_types = archive.getinfo("[Content_Types].xml")
                if content_types.file_size > 1024 * 1024:
                    raise ValueError("DOCX content types are too large")
                types = archive.read("[Content_Types].xml")
                if b"macroEnabled" in types or any(name.lower().endswith("vbaproject.bin") for name in names):
                    raise ValueError("Macro-enabled documents are not accepted as DOCX")
        except BadZipFile as exc:
            raise ValueError("DOCX archive is invalid") from exc
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("TXT resume must be UTF-8") from exc
        if any(ord(character) < 32 and character not in "\r\n\t" for character in text):
            raise ValueError("TXT resume contains binary control characters")
    return extension


def _uploads_root(output_dir: str | Path, *, create: bool) -> Path:
    root = Path(output_dir).resolve()
    uploads = root / "uploads"
    if create:
        uploads.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not uploads.is_dir() or uploads.is_symlink() or uploads.resolve().parent != root:
        raise UploadIntegrityError("Uploaded resume storage path is unavailable or unsafe")
    return uploads


def list_resumes(store, profile_id: str) -> list[dict]:
    store.profile(profile_id)
    with store.engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(m.uploaded_resumes).where(
            m.uploaded_resumes.c.profile_id == profile_id).order_by(m.uploaded_resumes.c.created_at.desc())).mappings()]


def save_resume(store, output_dir: str | Path, profile_id: str, filename: str,
                media_type: str, data: bytes) -> dict:
    store.profile(profile_id)
    extension = validate_resume(filename, media_type, data)
    digest = hashlib.sha256(data).hexdigest()
    identifier = identity("uploaded-resume", profile_id, digest)
    uploads = _uploads_root(output_dir, create=True)
    destination = uploads / f"{identifier}.{extension}"
    with tempfile.NamedTemporaryFile(dir=uploads, prefix=".upload-", delete=False) as staged:
        staged_path = Path(staged.name)
        staged.write(data)
        staged.flush()
        os.fsync(staged.fileno())
    try:
        try:
            os.link(staged_path, destination)
            os.chmod(destination, 0o600)
        except FileExistsError:
            if destination.is_symlink() or destination.stat().st_size != len(data) or hashlib.sha256(
                    destination.read_bytes()).hexdigest() != digest:
                raise UploadIntegrityError("Existing uploaded resume does not match its checksum")
    finally:
        staged_path.unlink(missing_ok=True)
    record = {"id": identifier, "profile_id": profile_id, "filename": filename, "format": extension,
              "media_type": media_type, "size_bytes": len(data), "sha256": digest,
              "status": "STORED_UNREVIEWED", "created_at": now()}
    with store.engine.begin() as connection:
        upsert(connection, m.uploaded_resumes, record)
        return dict(connection.execute(select(m.uploaded_resumes).where(
            m.uploaded_resumes.c.id == identifier, m.uploaded_resumes.c.profile_id == profile_id)).mappings().one())


def read_resume(store, output_dir: str | Path, profile_id: str, identifier: str) -> tuple[dict, bytes]:
    with store.engine.connect() as connection:
        record = dict(connection.execute(select(m.uploaded_resumes).where(
            m.uploaded_resumes.c.id == identifier, m.uploaded_resumes.c.profile_id == profile_id)).mappings().one())
    if record["format"] not in MEDIA_TYPES:
        raise UploadIntegrityError("Uploaded resume format metadata is unsafe")
    uploads = _uploads_root(output_dir, create=False)
    path = uploads / f"{record['id']}.{record['format']}"
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(uploads):
        raise UploadIntegrityError("Uploaded resume is missing or path is unsafe")
    if path.stat().st_size != record["size_bytes"] or record["size_bytes"] > MAX_UPLOAD_BYTES:
        raise UploadIntegrityError("Uploaded resume size changed")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise UploadIntegrityError("Uploaded resume checksum changed")
    return record, data
