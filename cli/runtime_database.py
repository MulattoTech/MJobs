"""Load the provisioned runtime database credential before executing a container command."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class RuntimeDatabaseError(ValueError):
    """The runtime credential is missing or unsafe; never fall back to an administrator."""


def runtime_environment(environment: dict[str, str]) -> dict[str, str]:
    result = dict(environment)
    credential_file = result.get("MJOBS_DATABASE_URL_FILE")
    if not credential_file:
        return result
    try:
        if result.get("MJOBS_DATABASE_URL"):
            raise ValueError("ambiguous credential sources")
        path = Path(credential_file)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            raise ValueError("unsafe credential file")
        value = path.read_text(encoding="utf-8")
        url = make_url(value)
        if (value != value.strip() or any(char in value for char in "\r\n\0")
                or url.drivername != "postgresql+psycopg" or url.username != "mjobs_runtime"
                or not url.password or not url.host or not url.database):
            raise ValueError("invalid runtime credential")
    except (OSError, UnicodeError, ValueError, ArgumentError):
        raise RuntimeDatabaseError("Runtime database credential unavailable; run the migration/provision service") from None
    result["MJOBS_DATABASE_URL"] = value
    return result


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if not arguments:
        print("Runtime command is required", file=sys.stderr)
        return 2
    try:
        environment = runtime_environment(dict(os.environ))
    except RuntimeDatabaseError as error:
        print(str(error), file=sys.stderr)
        return 2
    os.execvpe(arguments[0], arguments, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
