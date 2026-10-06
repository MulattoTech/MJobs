"""Create only the project's ignored local database secret, without printing it."""
import os
from pathlib import Path
import secrets
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def initialize(path: Path | None = None, secret_factory=None) -> bool:
    """Publish a complete private .env atomically and only if no file exists."""
    destination = Path(path) if path is not None else ROOT / ".env"
    if os.path.lexists(destination):
        return False

    factory = secret_factory or secrets.token_urlsafe
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{destination.name}.init-", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write("POSTGRES_PASSWORD=" + factory(32) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A same-directory hard link is atomic create-if-absent: unlike replace,
            # it cannot overwrite configuration installed by a concurrent writer.
            os.link(temporary, destination)
        except FileExistsError:
            return False
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    if initialize():
        print("Created project-local ignored .env")
    else:
        print("Existing .env preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
