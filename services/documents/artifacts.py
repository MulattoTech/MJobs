"""Crash-safe writes and conservative recovery for local document artifacts."""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from packages.persistence import models as m


DEFAULT_ORPHAN_AGE = timedelta(hours=24)
DEFAULT_QUARANTINE_RETENTION = timedelta(days=30)
QUARANTINE_DIRECTORY = ".quarantine"


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the platform supports directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Windows and some filesystems do not permit fsync on directories.
            pass
    finally:
        os.close(descriptor)


def atomic_generate(destination: str | Path, writer: Callable[[Path], None]) -> Path:
    """Generate a file beside its destination, then atomically replace it.

    A process death during ``writer`` can leave only a uniquely named temporary
    file. The previous destination remains readable until the replacement is
    fully written and fsynced.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        writer(temporary)
        with temporary.open("rb+") as output:
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def publish_directory(staged: str | Path, destination: str | Path) -> Path:
    """Publish a complete same-filesystem artifact directory without overwrite."""
    staged = Path(staged)
    destination = Path(destination)
    if not staged.is_dir() or staged.is_symlink():
        raise ValueError("Staged artifact directory is unavailable or unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Immutable artifact revision path already exists")
    os.replace(staged, destination)
    _fsync_directory(destination.parent)
    return destination


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("Artifact timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Artifact path is invalid")
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Artifact path is invalid")
    path = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Artifact path crosses a symbolic link")
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("Artifact path escapes its storage root")
    return path


def _document_references(connection) -> set[str]:
    referenced: set[str] = set()
    for row in connection.execute(select(m.documents.c.application_id, m.documents.c.files)).mappings():
        files = row["files"] or {}
        for relative in files.values():
            if isinstance(relative, str):
                referenced.add(Path(row["application_id"], relative).as_posix())
    for row in connection.execute(select(m.uploaded_resumes.c.id, m.uploaded_resumes.c.format)).mappings():
        if row["format"] in ("pdf", "docx", "txt"):
            referenced.add(Path("uploads", f"{row['id']}.{row['format']}").as_posix())
    return referenced


def _is_referenced(relative: str, referenced: set[str], is_directory: bool) -> bool:
    if relative in referenced:
        return True
    if is_directory:
        prefix = relative.rstrip("/") + "/"
        return any(value.startswith(prefix) for value in referenced)
    return False


def _only_generated_files(path: Path) -> bool:
    """Never quarantine a recognized revision containing unknown local files."""
    for item in path.rglob("*"):
        if item.is_symlink() or not item.is_file() or item.parent != path:
            return False
        if item.suffix.casefold() not in {".pdf", ".docx", ".txt", ".tmp"}:
            return False
    return True


def _classification(path: Path, root: Path, referenced: set[str]) -> tuple[str, bool]:
    relative = _relative(root, path)
    parts = Path(relative).parts
    if path.is_symlink():
        return "UNSAFE_SYMLINK", False
    if _is_referenced(relative, referenced, path.is_dir()):
        return ("REFERENCED_DIRECTORY" if path.is_dir() else "REFERENCED_ARTIFACT"), False
    if parts and parts[0] == "uploads":
        return "UNREFERENCED_UPLOAD_PROTECTED", False
    if any(part.casefold() in {"debug", "screenshots", "html-capture", "network-capture"}
           for part in parts):
        return "DEBUG_ARTIFACT_PROTECTED", False
    if path.is_dir() and len(parts) == 3 and parts[2].startswith(".") and ".staging-" in parts[2]:
        return "FAILED_GENERATION_STAGING", True
    if path.is_dir() and len(parts) == 3 and parts[1] == "answers" and parts[2].isdigit():
        if _only_generated_files(path):
            return "ORPHAN_ANSWER_EXPORT", True
    if path.is_dir() and len(parts) == 3 and parts[1] != "answers" and parts[2].isdigit():
        if any(item.is_file() and item.suffix.casefold() in {".pdf", ".docx", ".txt"}
               for item in path.iterdir()) and _only_generated_files(path):
            return "ORPHAN_GENERATED_REVISION", True
    if path.is_file() and len(parts) >= 4 and path.name.endswith(".tmp"):
        return "INCOMPLETE_TEMPORARY_FILE", True
    if path.is_file() and len(parts) >= 4 and path.suffix.casefold() in {".pdf", ".docx", ".txt"}:
        return "ORPHAN_GENERATED_FILE", True
    return "UNCLASSIFIED_PROTECTED", False


def _artifact_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(b"MJobs artifact tree v1\0")
    total = 0
    if path.is_symlink():
        raise ValueError("Symbolic links cannot be quarantined")
    if path.is_file():
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest(), len(data)
    if not path.is_dir():
        raise ValueError("Artifact is unavailable")
    for item in sorted(path.rglob("*"), key=lambda value: value.as_posix().casefold()):
        if item.is_symlink():
            raise ValueError("Artifact tree contains a symbolic link")
        if item.is_file():
            relative = item.relative_to(path).as_posix().encode("utf-8")
            data = item.read_bytes()
            digest.update(relative + b"\0" + len(data).to_bytes(8, "big") + data)
            total += len(data)
    return digest.hexdigest(), total


def _scan_with_connection(connection, root: Path, now: datetime,
                          minimum_age: timedelta) -> list[dict]:
    referenced = _document_references(connection)
    quarantine = root / QUARANTINE_DIRECTORY
    results: list[dict] = []

    def visit(path: Path) -> None:
        if path == quarantine or quarantine in path.parents:
            return
        try:
            classification, eligible_class = _classification(path, root, referenced)
            stat = path.lstat()
        except (OSError, ValueError):
            return
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        age = now - modified
        eligible = eligible_class and age >= minimum_age and not path.is_symlink()
        if path.is_dir() and eligible_class:
            try:
                digest, size = _artifact_digest(path)
            except (OSError, ValueError):
                classification, eligible, digest, size = "UNSAFE_ARTIFACT_TREE", False, None, 0
            if digest:
                relative = _relative(root, path)
                artifact_id = hashlib.sha256(
                    f"artifact-v1\0{relative}\0{digest}".encode("utf-8")
                ).hexdigest()
                results.append({"artifact_id": artifact_id, "path": relative,
                                "classification": classification, "sha256": digest,
                                "size_bytes": size, "modified_at": modified.isoformat(),
                                "eligible": eligible})
                return
        elif path.is_file() and eligible_class:
            try:
                digest, size = _artifact_digest(path)
                relative = _relative(root, path)
                artifact_id = hashlib.sha256(
                    f"artifact-v1\0{relative}\0{digest}".encode("utf-8")
                ).hexdigest()
                results.append({"artifact_id": artifact_id, "path": relative,
                                "classification": classification, "sha256": digest,
                                "size_bytes": size, "modified_at": modified.isoformat(),
                                "eligible": eligible})
            except (OSError, ValueError):
                results.append({"path": _relative(root, path),
                                "classification": "UNSAFE_ARTIFACT", "eligible": False})
            return
        if path.is_dir() and not path.is_symlink():
            results.append({"path": _relative(root, path), "classification": classification,
                            "eligible": False})
            try:
                children = sorted(path.iterdir(), key=lambda value: value.name.casefold())
            except OSError:
                return
            for child in children:
                visit(child)
        elif path.is_symlink():
            results.append({"path": _relative(root, path), "classification": classification,
                            "eligible": False})
        else:
            results.append({"path": _relative(root, path), "classification": classification,
                            "eligible": False})

    for child in sorted(root.iterdir(), key=lambda value: value.name.casefold()):
        visit(child)
    return results


def scan_artifacts(store, output_dir: str | Path, *, now: datetime | None = None,
                   minimum_age: timedelta = DEFAULT_ORPHAN_AGE) -> list[dict]:
    """Classify local artifacts; only old, recognized generated orphans qualify."""
    root = Path(output_dir)
    if root.is_symlink():
        raise ValueError("Artifact root cannot be a symbolic link")
    root = root.resolve()
    if not root.is_dir() or minimum_age < timedelta(0):
        raise ValueError("Artifact root or minimum age is invalid")
    with store.engine.connect() as connection:
        return _scan_with_connection(connection, root, _utc(now), minimum_age)


def _quarantine_root(root: Path) -> Path:
    path = root / QUARANTINE_DIRECTORY
    if path.is_symlink():
        raise ValueError("Quarantine directory cannot be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _write_manifest(path: Path, manifest: dict) -> None:
    payload = json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    atomic_generate(path, lambda temporary: temporary.write_bytes(payload))


def _read_manifest(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Quarantine manifest is invalid")
    return value


def _record_audit(store, quarantine_id: str, action: str, payload: dict,
                  occurred_at: datetime) -> None:
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        existing = connection.execute(select(m.audit.c.id).where(
            m.audit.c.entity_id == quarantine_id, m.audit.c.action == action).limit(1)).first()
        if existing is None:
            store._audit(connection, quarantine_id, action, payload | {"occurred_at": occurred_at.isoformat()})


def quarantine_artifact(store, output_dir: str | Path, artifact_id: str, *,
                        now: datetime | None = None) -> dict:
    """Move one old eligible artifact into a recoverable, audited quarantine."""
    root = Path(output_dir)
    if root.is_symlink():
        raise ValueError("Artifact root cannot be a symbolic link")
    root = root.resolve()
    now = _utc(now)
    matches = [item for item in scan_artifacts(store, root, now=now)
               if item.get("artifact_id") == artifact_id and item.get("eligible")]
    if len(matches) != 1:
        raise ValueError("Artifact is missing, referenced, too new, or not eligible for quarantine")
    item = matches[0]
    source = _safe_path(root, item["path"])
    digest, size = _artifact_digest(source)
    if digest != item["sha256"] or size != item["size_bytes"]:
        raise ValueError("Artifact changed during quarantine review")
    quarantine_root = _quarantine_root(root)
    quarantine_id = uuid4().hex
    destination = quarantine_root / quarantine_id
    destination.mkdir(mode=0o700)
    manifest_path = quarantine_root / f"{quarantine_id}.json"
    manifest = {"schema_version": 1, "quarantine_id": quarantine_id,
                "artifact_id": artifact_id, "state": "PREPARING",
                "classification": item["classification"], "original_path": item["path"],
                "sha256": digest, "size_bytes": size, "modified_at": item["modified_at"],
                "quarantined_at": now.isoformat()}
    payload_path = destination / "artifact"
    audit_recorded = False
    try:
        _write_manifest(manifest_path, manifest)
        os.replace(source, payload_path)
        _fsync_directory(source.parent)
        moved_digest, moved_size = _artifact_digest(payload_path)
        if moved_digest != digest or moved_size != size:
            os.replace(payload_path, source)
            raise ValueError("Artifact bytes changed while being quarantined")
        manifest["state"] = "QUARANTINED"
        _write_manifest(manifest_path, manifest)
        _fsync_directory(destination)
        _record_audit(store, quarantine_id, "ARTIFACT_QUARANTINED",
                      {"artifact_id": artifact_id, "classification": item["classification"],
                       "sha256": digest, "size_bytes": size}, now)
        audit_recorded = True
        return {key: value for key, value in manifest.items() if key != "schema_version"}
    except BaseException:
        if payload_path.exists() and not source.exists():
            os.replace(payload_path, source)
        if source.exists() and not payload_path.exists() and not audit_recorded:
            manifest_path.unlink(missing_ok=True)
            _fsync_directory(quarantine_root)
        if source.exists() and destination.exists() and not any(destination.iterdir()):
            destination.rmdir()
        raise


def list_quarantined(output_dir: str | Path) -> list[dict]:
    root = Path(output_dir)
    if root.is_symlink():
        raise ValueError("Artifact root cannot be a symbolic link")
    root = root.resolve()
    directory = root / QUARANTINE_DIRECTORY
    if directory.is_symlink() or not directory.exists():
        return []
    result = []
    for manifest_path in sorted(directory.glob("*.json")):
        if manifest_path.is_symlink():
            continue
        try:
            manifest = _read_manifest(manifest_path)
            if manifest.get("quarantine_id") == manifest_path.stem:
                result.append({key: value for key, value in manifest.items()
                               if key != "schema_version"})
        except (OSError, ValueError, TypeError):
            continue
    return result


def _quarantine_entry(root: Path, quarantine_id: str) -> tuple[Path, Path, dict]:
    if not isinstance(quarantine_id, str) or len(quarantine_id) != 32 or any(
            character not in "0123456789abcdef" for character in quarantine_id):
        raise ValueError("Quarantine identifier is invalid")
    entry = _safe_path(root, f"{QUARANTINE_DIRECTORY}/{quarantine_id}")
    manifest_path = _safe_path(root, f"{QUARANTINE_DIRECTORY}/{quarantine_id}.json")
    if not manifest_path.is_file():
        raise ValueError("Quarantine manifest is unavailable")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Quarantine manifest is unavailable")
    manifest = _read_manifest(manifest_path)
    if manifest.get("quarantine_id") != quarantine_id:
        raise ValueError("Quarantine manifest does not match its identifier")
    payload = entry / "artifact"
    return entry, payload, manifest


def _managed_generated_path(relative: str, *, is_directory: bool,
                           classification: str) -> bool:
    """Limit restores to the generated layouts recognized by the inventory."""
    if not isinstance(relative, str) or "\\" in relative:
        return False
    parts = Path(relative).parts
    if len(parts) not in ({3} if is_directory else {4}):
        return False
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if parts[0] in {QUARANTINE_DIRECTORY, "uploads"}:
        return False
    if is_directory:
        if classification == "FAILED_GENERATION_STAGING":
            return ".staging-" in parts[2] and parts[2].startswith(".")
        return parts[2].isdigit()
    if not parts[2].isdigit():
        return False
    return parts[3].casefold().endswith((".pdf", ".docx", ".txt", ".tmp"))


def restore_quarantined(store, output_dir: str | Path, quarantine_id: str, *,
                        now: datetime | None = None) -> dict:
    """Restore a verified quarantined artifact without overwriting any current file."""
    root = Path(output_dir)
    if root.is_symlink():
        raise ValueError("Artifact root cannot be a symbolic link")
    root = root.resolve()
    now = _utc(now)
    entry, payload, manifest = _quarantine_entry(root, quarantine_id)
    if manifest.get("state") not in {"QUARANTINED", "PREPARING"}:
        raise ValueError("Artifact is not in a restorable state")
    destination = _safe_path(root, manifest.get("original_path"))
    if not _managed_generated_path(
            manifest.get("original_path"),
            is_directory=manifest.get("classification") in {
                "FAILED_GENERATION_STAGING", "ORPHAN_ANSWER_EXPORT",
                "ORPHAN_GENERATED_REVISION"},
            classification=manifest.get("classification")):
        raise ValueError("Quarantine manifest does not identify a managed generated artifact")
    if destination.exists() and not payload.exists():
        digest, size = _artifact_digest(destination)
        if digest == manifest.get("sha256") and size == manifest.get("size_bytes"):
            manifest["state"] = "RESTORED"
            manifest["restored_at"] = now.isoformat()
            _write_manifest(root / QUARANTINE_DIRECTORY / f"{quarantine_id}.json", manifest)
            _record_audit(store, quarantine_id, "ARTIFACT_QUARANTINED",
                          {"artifact_id": manifest["artifact_id"],
                           "classification": manifest["classification"],
                           "sha256": manifest["sha256"], "size_bytes": manifest["size_bytes"]}, now)
            _record_audit(store, quarantine_id, "ARTIFACT_RESTORED",
                          {"artifact_id": manifest["artifact_id"], "sha256": digest,
                           "size_bytes": size}, now)
            return {key: value for key, value in manifest.items() if key != "schema_version"}
        raise FileExistsError("Restore will not overwrite an existing artifact")
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink() and not any(destination.iterdir()):
            destination.rmdir()
        else:
            raise FileExistsError("Restore will not overwrite an existing artifact")
    if not payload.exists():
        raise ValueError("Quarantined artifact payload is missing")
    digest, size = _artifact_digest(payload)
    if digest != manifest.get("sha256") or size != manifest.get("size_bytes"):
        raise ValueError("Quarantined artifact failed its integrity check")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(payload, destination)
    _fsync_directory(destination.parent)
    manifest["state"] = "RESTORED"
    manifest["restored_at"] = now.isoformat()
    try:
        _write_manifest(root / QUARANTINE_DIRECTORY / f"{quarantine_id}.json", manifest)
        _record_audit(store, quarantine_id, "ARTIFACT_QUARANTINED",
                      {"artifact_id": manifest["artifact_id"],
                       "classification": manifest["classification"],
                       "sha256": digest, "size_bytes": size}, now)
        _record_audit(store, quarantine_id, "ARTIFACT_RESTORED",
                      {"artifact_id": manifest["artifact_id"], "sha256": digest,
                       "size_bytes": size}, now)
    except BaseException:
        manifest["state"] = "QUARANTINED"
        manifest.pop("restored_at", None)
        try:
            _write_manifest(root / QUARANTINE_DIRECTORY / f"{quarantine_id}.json", manifest)
        except OSError:
            pass
        if destination.exists() and not payload.exists():
            os.replace(destination, payload)
        raise
    return {key: value for key, value in manifest.items() if key != "schema_version"}


def purge_expired_quarantine(store, output_dir: str | Path, *,
                             now: datetime | None = None,
                             retention: timedelta = DEFAULT_QUARANTINE_RETENTION) -> list[str]:
    """Purge only integrity-checked quarantined payloads past the retention age."""
    root = Path(output_dir)
    if root.is_symlink():
        raise ValueError("Artifact root cannot be a symbolic link")
    root = root.resolve()
    now = _utc(now)
    if retention < timedelta(0):
        raise ValueError("Quarantine retention cannot be negative")
    purged: list[str] = []
    for summary in list_quarantined(root):
        quarantine_id = summary["quarantine_id"]
        if summary.get("state") not in {"QUARANTINED", "PURGE_PENDING"}:
            continue
        try:
            quarantined_at = datetime.fromisoformat(summary["quarantined_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - _utc(quarantined_at) < retention:
            continue
        entry, payload, manifest = _quarantine_entry(root, quarantine_id)
        if payload.exists():
            digest, size = _artifact_digest(payload)
            if digest != manifest.get("sha256") or size != manifest.get("size_bytes"):
                continue
        manifest["state"] = "PURGE_PENDING"
        manifest["purge_requested_at"] = now.isoformat()
        _write_manifest(root / QUARANTINE_DIRECTORY / f"{quarantine_id}.json", manifest)
        _record_audit(store, quarantine_id, "ARTIFACT_PURGE_REQUESTED",
                      {"artifact_id": manifest["artifact_id"], "sha256": manifest["sha256"],
                       "size_bytes": manifest["size_bytes"]}, now)
        if entry.exists():
            shutil.rmtree(entry)
            _fsync_directory(entry.parent)
        manifest["state"] = "PURGED"
        manifest["purged_at"] = now.isoformat()
        _write_manifest(root / QUARANTINE_DIRECTORY / f"{quarantine_id}.json", manifest)
        _record_audit(store, quarantine_id, "ARTIFACT_PURGED",
                      {"artifact_id": manifest["artifact_id"], "sha256": manifest["sha256"],
                       "size_bytes": manifest["size_bytes"]}, now)
        purged.append(quarantine_id)
    return purged
