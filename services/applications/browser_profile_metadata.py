"""Nonsecret observations for an already admitted persistent browser profile.

The sidecar is not a session selector, authentication proof, browser lease, or
submission authority. Callers supply the validated application root and pinned
profile. Only an actual successful context launch records use. A legacy profile's
created_at is its first metadata registration, not an invented directory age.

Reads and writes are byte bounded, not a hard wall-clock filesystem guarantee.
Existing browser/operation admission serializes normal writers; this is not a
new cross-process lock or protection against hostile ancestor replacement.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit, urlunsplit

from services.applications.browser_profiles import (
    _unalias, browser_profile_path, validate_browser_profile_name,
)

METADATA_FILENAME = "mjobs-profile-metadata.json"
MAX_METADATA_BYTES = 4096
_LOG = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PROVIDER = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_FIELDS = frozenset({
    "schema_version", "profile_id", "application_id", "profile_name",
    "provider", "site", "status", "last_used", "likely_authenticated", "created_at",
})
_UNAVAILABLE = "BROWSER_PROFILE_METADATA_UNAVAILABLE"


class BrowserProfileMetadataError(ValueError):
    """A value-free refusal; never attach private paths or rejected content."""


def _refuse():
    raise BrowserProfileMetadataError(_UNAVAILABLE)


def _origin(value):
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)):
        _refuse()
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        _refuse()
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and not (
            (parsed.scheme == "http" and port == 80)
            or (parsed.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, "", "", ""))


def _scope(application_root, profile, provider, site):
    root = Path(application_root).absolute()
    selected = Path(profile).absolute()
    _unalias(root)
    _unalias(selected)
    expected = browser_profile_path(root, initialize=False)
    if not root.is_dir() or not selected.is_dir():
        _refuse()
    if expected.resolve(strict=True) != selected.resolve(strict=True):
        _refuse()
    for path in (root, selected, expected):
        _unalias(path)
    relative = expected.relative_to(root / "browser-profile")
    if not relative.parts:
        name = "default"
    elif len(relative.parts) == 1:
        name = validate_browser_profile_name(relative.parts[0])
    else:
        _refuse()
    application_id = root.name
    if (not _IDENTIFIER.fullmatch(application_id)
            or not isinstance(provider, str) or not _PROVIDER.fullmatch(provider)):
        _refuse()
    identity = hashlib.sha256(json.dumps(
        ["mjobs-browser-profile-v1", application_id, name],
        separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    binding = {
        "profile_id": identity, "application_id": application_id,
        "profile_name": name, "provider": provider, "site": _origin(site),
    }
    directory = selected.stat()
    return selected / METADATA_FILENAME, binding, (directory.st_dev, directory.st_ino)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse()
        result[key] = value
    return result


def _time(value):
    if not isinstance(value, str) or len(value) != 27 or not value.endswith("Z"):
        _refuse()
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != value:
        _refuse()
    return parsed


def _validate(record, binding):
    if (not isinstance(record, dict) or set(record) != _FIELDS
            or type(record["schema_version"]) is not int or record["schema_version"] != 1
            or any(record[key] != value for key, value in binding.items())):
        _refuse()
    if record["status"] == "AUTHENTICATION_UNKNOWN":
        if record["likely_authenticated"] is not None:
            _refuse()
    elif record["status"] == "AUTH_REQUIRED":
        if record["likely_authenticated"] is not False:
            _refuse()
    else:
        _refuse()
    if _time(record["last_used"]) < _time(record["created_at"]):
        _refuse()
    return record


def _read(path, binding):
    _unalias(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or (os.name == "posix" and stat.S_IMODE(before.st_mode) & 0o077)):
        _refuse()
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_size > MAX_METADATA_BYTES
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            _refuse()
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        _refuse()
    return _validate(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique), binding)


def read_browser_profile_metadata(application_root, profile, *, provider, site):
    """Read the exact bound record without creating or repairing any state."""
    try:
        path, binding, _ = _scope(application_root, profile, provider, site)
        return _read(path, binding)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        # OperationTimedOut is a TimeoutError/OSError. Never downgrade it.
        if isinstance(error, TimeoutError):
            raise
        raise BrowserProfileMetadataError(_UNAVAILABLE) from None


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write(application_root, profile, provider, site, scope, previous, record):
    path, binding, directory_identity = scope
    raw = (json.dumps(_validate(record, binding), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_METADATA_BYTES:
        _refuse()
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".mjobs-profile-metadata-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        current = _scope(application_root, profile, provider, site)
        if current != (path, binding, directory_identity) or _read(path, binding) != previous:
            _refuse()
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError as error:
                if isinstance(error, TimeoutError):
                    raise
                # An owned temporary-file cleanup failure cannot replace the
                # original schema/IO/control failure. No session file is touched.
                pass


def _observe(application_root, profile, *, provider, site, authentication_required):
    scope = _scope(application_root, profile, provider, site)
    path, binding, _ = scope
    previous = _read(path, binding)
    if authentication_required:
        # Do not invent a launch time if its earlier recording was unavailable.
        if previous is None:
            _refuse()
        record = {**previous, "status": "AUTH_REQUIRED", "likely_authenticated": False}
    else:
        stamp = _utc_now()
        if previous is not None and _time(stamp) < _time(previous["last_used"]):
            _refuse()
        record = {
            "schema_version": 1, **binding,
            "status": "AUTHENTICATION_UNKNOWN", "last_used": stamp,
            "likely_authenticated": None,
            "created_at": previous["created_at"] if previous is not None else stamp,
        }
    _write(application_root, profile, provider, site, scope, previous, record)
    return True


def _record(application_root, profile, *, provider, site, authentication_required):
    try:
        return _observe(application_root, profile, provider=provider, site=site,
                        authentication_required=authentication_required)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        if isinstance(error, TimeoutError):
            raise
        # Never log exception text, scope values, URLs, IDs, or filesystem paths.
        # No workflow callback, browser action, or deadline check is inside this
        # optional-IO catch. Runtime/control exceptions propagate unchanged.
        _LOG.warning(_UNAVAILABLE)
        return False


def record_browser_profile_use(application_root, profile, *, provider, site):
    """Observe successful launch only; window closure never proves authentication."""
    return _record(application_root, profile, provider=provider, site=site,
                   authentication_required=False)


def record_browser_profile_auth_required(application_root, profile, *, provider, site):
    """Record an observed login barrier without advancing last_used."""
    return _record(application_root, profile, provider=provider, site=site,
                   authentication_required=True)
