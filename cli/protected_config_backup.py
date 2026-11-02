"""Explicit, encrypted configuration backups; never apply recovered configuration.

The current user and administrators are trusted, as in migration_precondition.
All plaintext is bounded, authenticated before restore, and written only into
new private files. The independent recovery key is never part of the archive.
"""
from __future__ import annotations

import argparse
import base64
import functools
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"MJPC1\x00"
NONCE_BYTES = 12
KEY_BYTES = 32
MAX_FILES = 8
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
NAME = re.compile(r"(?:\.env|compose\.[a-z0-9][a-z0-9_-]{0,63}\.ya?ml)\Z", re.ASCII)


class ProtectedConfigError(ValueError):
    """Fixed, value-free error safe for command output."""


def _safe_errors(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ProtectedConfigError:
            raise
        except Exception:
            raise ProtectedConfigError("Protected configuration operation failed") from None
    return guarded


def _path(value: str | Path) -> Path:
    result = Path(value).absolute()
    if ".." in result.parts:
        raise ProtectedConfigError("Configuration paths must not contain parent traversal")
    return result


def _no_links(path: Path, *, new: bool = False) -> None:
    for node in (path, *path.parents):
        try:
            info = node.lstat()
        except FileNotFoundError:
            if new and node == path:
                continue
            raise ProtectedConfigError("Configuration path is unavailable") from None
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ProtectedConfigError("Configuration paths must not contain links")


def _private_parent(path: Path) -> None:
    # Lazy import keeps format validation independent of database tooling.
    from cli.migration_precondition import require_private_parent
    _no_links(path)
    require_private_parent(path)


def _private_file(path: Path) -> None:
    from cli.migration_precondition import require_private_file
    _no_links(path)
    require_private_file(path)


def _identity(info: os.stat_result, *, cross_api: bool = False) -> tuple:
    common = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
              info.st_size, info.st_mtime_ns)
    # Windows path and descriptor stat APIs can report different ctime
    # meanings for one unchanged file. Keep ctime for every same-API check,
    # and for all POSIX comparisons; omit it only across Windows APIs.
    if cross_api and os.name == "nt":
        return common
    return (*common, info.st_ctime_ns)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _read_snapshot(path: Path, limit: int) -> tuple[bytes, os.stat_result]:
    _private_file(path)
    before = path.lstat()
    if before.st_size > limit:
        raise ProtectedConfigError("Configuration file exceeds its size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or _identity(before, cross_api=True) != _identity(opened, cross_api=True)):
            raise ProtectedConfigError("Configuration file changed while being read")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    _private_file(path)
    final_path = path.lstat()
    if (len(data) > limit or _identity(opened) != _identity(after)
            or _identity(before) != _identity(final_path)
            or _identity(after, cross_api=True) != _identity(final_path, cross_api=True)):
        raise ProtectedConfigError("Configuration file changed or exceeded its size limit")
    return data, final_path


def _read_private(path: Path, limit: int) -> bytes:
    return _read_snapshot(path, limit)[0]


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _remove_owned_file(path: Path, original: os.stat_result) -> None:
    try:
        _no_links(path.parent)
        if _same_file(path.lstat(), original):
            path.unlink()
    except OSError:
        pass
    except ProtectedConfigError:
        pass


def _publish_new(destination: Path, data: bytes) -> os.stat_result:
    """Publish a private single-link file and return its original identity."""
    _private_parent(destination.parent)
    _no_links(destination, new=True)
    if os.path.lexists(destination):
        raise ProtectedConfigError("Configuration output already exists")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".mjobs-config-", suffix=".tmp", dir=destination.parent)
    temporary = Path(temporary_name)
    original = os.fstat(descriptor)
    published = False
    complete = False
    try:
        # Check the empty file before writing plaintext/key bytes, including NTFS
        # DACLs. chmod alone is not a Windows privacy guarantee.
        _private_file(temporary)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _private_file(temporary)
        _private_parent(destination.parent)
        if not _same_file(temporary.lstat(), original):
            raise ProtectedConfigError("Configuration output changed before publication")
        # Same-directory hard link is atomic create-if-absent on supported
        # POSIX/NTFS filesystems; never replace a concurrent writer's file.
        os.link(temporary, destination, follow_symlinks=False)
        published = True
        if not _same_file(temporary.lstat(), original):
            raise ProtectedConfigError("Configuration staging file changed")
        # Removing the staging link is part of publication, not best-effort
        # cleanup: a two-link output is intentionally unreadable by our reader.
        temporary.unlink()
        if os.path.lexists(temporary):
            raise ProtectedConfigError("Configuration staging cleanup is incomplete")
        _private_file(destination)
        observed = destination.lstat()
        if (not _same_file(observed, original) or not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1):
            raise ProtectedConfigError("Configuration output changed during publication")
        _sync_directory(destination.parent)
        complete = True
        return original
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if published and not complete:
            _remove_owned_file(destination, original)
        _remove_owned_file(temporary, original)


def _names(values) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_FILES:
        raise ProtectedConfigError("Select between one and eight configuration files")
    if any(not isinstance(name, str) or not NAME.fullmatch(name) for name in values):
        raise ProtectedConfigError("Only .env and flat private Compose filenames are supported")
    if len({name.casefold() for name in values}) != len(values):
        raise ProtectedConfigError("Configuration filenames must be unique")
    return tuple(values)


def _separate(key: Path, archive: Path, other: Path | None = None) -> None:
    if key.is_relative_to(archive.parent) or archive.is_relative_to(key.parent):
        raise ProtectedConfigError("Recovery key and archive require separate private directories")
    if other is not None and (key.is_relative_to(other) or other.is_relative_to(key.parent)):
        raise ProtectedConfigError("Recovery key must be stored separately from configuration")


def _key(path: Path) -> bytes:
    value = _read_private(path, KEY_BYTES)
    if len(value) != KEY_BYTES:
        raise ProtectedConfigError("Recovery key must contain exactly 32 bytes")
    return value


def _encode(files: dict[str, bytes]) -> bytes:
    _names(list(files))
    if (any(not isinstance(value, bytes) or len(value) > MAX_FILE_BYTES for value in files.values())
            or sum(map(len, files.values())) > MAX_TOTAL_BYTES):
        raise ProtectedConfigError("Configuration payload exceeds its size limit")
    return json.dumps({"schema_version": 1, "files": [
        {"name": name, "data": base64.b64encode(value).decode("ascii")}
        for name, value in files.items()
    ]}, separators=(",", ":")).encode("ascii")


def _unique_fields(entries):
    result = {}
    for name, value in entries:
        if name in result:
            raise ProtectedConfigError("Configuration archive contains duplicate fields")
        result[name] = value
    return result


def _decode(data: bytes) -> dict[str, bytes]:
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE_BYTES:
        raise ProtectedConfigError("Configuration archive exceeds its size limit")
    try:
        payload = json.loads(data, object_pairs_hook=_unique_fields)
        if (not isinstance(payload, dict) or set(payload) != {"schema_version", "files"}
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or not isinstance(payload["files"], list)):
            raise ValueError()
        records = payload["files"]
        if not 1 <= len(records) <= MAX_FILES:
            raise ValueError()
        names = _names([item["name"] for item in records])
        result = {}
        total = 0
        for name, item in zip(names, records, strict=True):
            if set(item) != {"name", "data"} or not isinstance(item["data"], str):
                raise ValueError()
            if len(item["data"]) > 4 * ((MAX_FILE_BYTES + 2) // 3):
                raise ValueError()
            value = base64.b64decode(item["data"].encode("ascii"), validate=True)
            total += len(value)
            if len(value) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError()
            result[name] = value
        return result
    except ProtectedConfigError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ProtectedConfigError("Configuration archive payload is invalid") from None


def _encrypt(files: dict[str, bytes], key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ProtectedConfigError("Recovery key must contain exactly 32 bytes")
    nonce = secrets.token_bytes(NONCE_BYTES)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, _encode(files), MAGIC)


def _decrypt(envelope: bytes, key: bytes) -> dict[str, bytes]:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ProtectedConfigError("Recovery key must contain exactly 32 bytes")
    if (not isinstance(envelope, bytes) or len(envelope) > MAX_ARCHIVE_BYTES
            or len(envelope) <= len(MAGIC) + NONCE_BYTES + 16 or not envelope.startswith(MAGIC)):
        raise ProtectedConfigError("Configuration archive envelope is invalid")
    try:
        offset = len(MAGIC)
        plaintext = AESGCM(key).decrypt(envelope[offset:offset + NONCE_BYTES],
                                        envelope[offset + NONCE_BYTES:], MAGIC)
    except Exception:
        raise ProtectedConfigError("Configuration archive authentication failed") from None
    return _decode(plaintext)


@_safe_errors
def generate_key(key_file: str | Path) -> None:
    _publish_new(_path(key_file), secrets.token_bytes(KEY_BYTES))


@_safe_errors
def backup_config(source_root: str | Path, filenames: list[str], key_file: str | Path,
                  archive: str | Path) -> int:
    source, key_path, output = map(_path, (source_root, key_file, archive))
    names = _names(filenames)
    _no_links(source)
    if not source.is_dir() or output.is_relative_to(source):
        raise ProtectedConfigError("Archive must be outside the configuration source directory")
    _separate(key_path, output, source)
    key = _key(key_path)
    files = {}
    snapshots = {}
    total = 0
    for name in names:
        value, snapshot = _read_snapshot(source / name, MAX_FILE_BYTES)
        total += len(value)
        if total > MAX_TOTAL_BYTES:
            raise ProtectedConfigError("Configuration payload exceeds its size limit")
        files[name] = value
        snapshots[name] = snapshot
    for name, snapshot in snapshots.items():
        path = source / name
        _private_file(path)
        if _identity(path.lstat()) != _identity(snapshot):
            raise ProtectedConfigError("Configuration changed during backup")
    _publish_new(output, _encrypt(files, key))
    return len(files)


def _load(archive: Path, key: Path) -> dict[str, bytes]:
    _separate(key, archive)
    return _decrypt(_read_private(archive, MAX_ARCHIVE_BYTES), _key(key))


@_safe_errors
def verify_archive(archive: str | Path, key_file: str | Path) -> int:
    return len(_load(_path(archive), _path(key_file)))


@_safe_errors
def restore_config(archive: str | Path, key_file: str | Path, destination: str | Path) -> int:
    archive_path, key_path, output = map(_path, (archive, key_file, destination))
    _separate(key_path, archive_path, output)
    _no_links(output, new=True)
    if os.path.lexists(output):
        raise ProtectedConfigError("Configuration restore destination already exists")
    # Every filename and byte is authenticated and validated before mkdir.
    files = _load(archive_path, key_path)
    _private_parent(output.parent)
    output.mkdir(mode=0o700, exist_ok=False)
    original = output.lstat()
    created = []
    complete = False
    try:
        _private_parent(output)
        for name, data in files.items():
            if not _same_file(output.lstat(), original):
                raise ProtectedConfigError("Configuration restore directory changed")
            path = output / name
            published_identity = _publish_new(path, data)
            created.append((path, published_identity))
            if not _same_file(path.lstat(), published_identity):
                raise ProtectedConfigError("Restored configuration file changed")
            _private_file(path)
        if not _same_file(output.lstat(), original):
            raise ProtectedConfigError("Configuration restore directory changed")
        if any(not _same_file(path.lstat(), identity) for path, identity in created):
            raise ProtectedConfigError("Restored configuration file changed")
        _sync_directory(output.parent)
        complete = True
        return len(files)
    finally:
        if not complete:
            # Only remove this invocation's objects. Never rmtree a pathname
            # that could have acquired somebody else's files or changed identity.
            try:
                _no_links(output)
                if _same_file(output.lstat(), original):
                    for path, identity in reversed(created):
                        _remove_owned_file(path, identity)
                    output.rmdir()
            except (OSError, ProtectedConfigError):
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Encrypted backup of explicitly selected private configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen", help="Create an independent private 32-byte binary recovery key")
    keygen.add_argument("--key-file", required=True)
    backup = commands.add_parser("backup", help="Encrypt selected files without executing their contents")
    backup.add_argument("--source-root", required=True)
    backup.add_argument("--file", action="append", required=True, dest="filenames")
    backup.add_argument("--key-file", required=True)
    backup.add_argument("--archive", required=True)
    for operation in ("verify", "restore"):
        command = commands.add_parser(operation)
        command.add_argument("--key-file", required=True)
        command.add_argument("--archive", required=True)
        if operation == "restore":
            command.add_argument("--destination", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "keygen":
            generate_key(arguments.key_file)
            print("Created private configuration recovery key")
        elif arguments.command == "backup":
            count = backup_config(arguments.source_root, arguments.filenames, arguments.key_file, arguments.archive)
            print(f"Created encrypted configuration archive ({count} files)")
        elif arguments.command == "verify":
            count = verify_archive(arguments.archive, arguments.key_file)
            print(f"Verified encrypted configuration archive ({count} files)")
        else:
            count = restore_config(arguments.archive, arguments.key_file, arguments.destination)
            print(f"Restored private configuration to a fresh directory ({count} files)")
        return 0
    except ProtectedConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("Protected configuration operation failed; check paths, private permissions, key, and archive", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
