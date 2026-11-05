"""Named, application-isolated browser profiles with restart-stable selection.

Only a short nonsecret label is configurable. Callers cannot select a host path,
reuse another application's cookies, or change an existing application's profile
by changing the process environment. Legacy sessions retain their original path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat


_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_SELECTION = "browser-profile-selection.json"
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


def validate_browser_profile_name(value: object) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or value in _RESERVED:
        raise ValueError("MJOBS_BROWSER_PROFILE_INVALID")
    return value


def configured_browser_profile() -> str:
    return validate_browser_profile_name(os.environ.get("MJOBS_BROWSER_PROFILE", "default"))


def _unalias(path: Path) -> None:
    # Check lexical components before resolving, including Windows junctions.
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if (candidate.is_symlink()
                or (hasattr(candidate, "is_junction") and candidate.is_junction())):
            raise ValueError("BROWSER_PROFILE_UNSAFE")


def _read_selection(path: Path) -> str:
    _unalias(path)
    try:
        # Reject special files both before and after open. NONBLOCK prevents a
        # raced FIFO from wedging a worker; NOFOLLOW also protects the leaf.
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 256:
                raise ValueError()
            raw = stream.read(257)
        if len(raw) > 256:
            raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "name"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise ValueError()
        return validate_browser_profile_name(value["name"])
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError("BROWSER_PROFILE_SELECTION_INVALID") from None


def _profile_directory(path: Path, *, initialize: bool = False) -> bool:
    """Admit only real, private session directories; never chmod old state."""
    _unalias(path)
    if initialize:
        # Create each session-bearing level explicitly. With parents=True, only
        # the final named child would receive this mode.
        path.mkdir(mode=0o700, exist_ok=True)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if initialize:
            raise ValueError("BROWSER_PROFILE_UNSAFE") from None
        return False
    if (not stat.S_ISDIR(metadata.st_mode)
            or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077)):
        raise ValueError("BROWSER_PROFILE_UNSAFE")
    _unalias(path)
    return True


def browser_profile_path(application_root: Path, *, initialize: bool = False) -> Path:
    """Resolve a pinned selection; initialize only before an authorized launch.

    The exclusive small manifest never contains credentials. A malformed or
    partial manifest fails closed; it is never replaced with a different session
    selection. Every cookie directory remains below the backup exclusion root.
    Existing session directories must be private on POSIX; Windows mode bits do
    not establish ACL isolation. Read-only lookup never creates missing state.
    """
    try:
        root = Path(application_root).absolute()
        selection = root / _SELECTION
        profile_root = root / "browser-profile"
        for path in (root, selection):
            _unalias(path)
        existing_profile = _profile_directory(profile_root)
        if selection.exists():
            name = _read_selection(selection)
        elif initialize:
            # An existing unlabelled session predates selection support.
            name = "default" if existing_profile else configured_browser_profile()
            root.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(selection, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                name = _read_selection(selection)
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"schema_version": 1, "name": name}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
        else:
            name = "default"
        profile = profile_root if name == "default" else profile_root / name
        _profile_directory(profile_root, initialize=initialize)
        if profile != profile_root:
            _profile_directory(profile, initialize=initialize)
        return profile
    except OSError:
        # OS errors can contain private local paths. Consumers already handle
        # the fixed unsafe-profile refusal; preserve that public boundary.
        raise ValueError("BROWSER_PROFILE_UNSAFE") from None
