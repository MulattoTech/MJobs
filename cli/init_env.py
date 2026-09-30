"""Create only the project's ignored local database secret, without printing it."""
import os
from pathlib import Path
import secrets

path = Path(__file__).resolve().parents[1] / ".env"
try:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    print("Existing .env preserved")
else:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("POSTGRES_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
    print("Created project-local ignored .env")
