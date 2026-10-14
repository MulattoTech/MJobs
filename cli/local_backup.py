"""Back up quiescent local data and restore only into a fresh Compose project."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from packages.persistence import models
from packages.persistence.store import Store
from services.credentials.vault import CredentialCipher, CredentialConfigurationError


PROJECT = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ROOT = Path(__file__).resolve().parents[1]


class BackupError(ValueError):
    """An archive, Docker operation, or recovery precondition failed."""


WRITERS = ("api", "worker", "source-worker", "document-worker", "ai-worker", "migrate",
           "scheduler", "company-discovery-worker")
MAX_CAPTURED_STDOUT_BYTES = 2 * 1024 * 1024
_OUTPUT_CHUNK_BYTES = 64 * 1024
MAX_COMPOSE_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_RUNTIME_SNAPSHOT_BYTES = 16 * 1024
MAX_RECEIPT_SNAPSHOT_BYTES = 2 * 1024 * 1024
RECEIPT_PATH = "mock/receipts.json"
CONFIGURATION_FILES = frozenset((
    "configuration/compose.yml",
    "configuration/runtime.json",
))
RUNTIME_SETTING_BOUNDS = {
    "MJOBS_SOURCE_COLLECTION_CONCURRENCY": (1, 4),
    "MJOBS_COMPANY_DISCOVERY_CONCURRENCY": (1, 2),
    "MJOBS_AI_CONCURRENCY": (1, 2),
    "MJOBS_DOCUMENT_GENERATION_CONCURRENCY": (1, 2),
    "MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": (1, 2),
    "MJOBS_TRANSIENT_RETENTION_INTERVAL_SECONDS": (300, 2_592_000),
}
RUNTIME_SETTING_NAMES = frozenset((
    "MJOBS_OPERATION_MODE",
    "MJOBS_COMPOSE_MOCK",
    *RUNTIME_SETTING_BOUNDS.keys(),
))


def require_stopped_writers(states: dict[str, str]) -> None:
    """Require a quiescent application before pairing a dump with document files."""
    if states.get("postgres") != "running":
        raise BackupError("PostgreSQL must be running for a logical backup")
    for service in WRITERS:
        if states.get(service) not in ("exited", "created"):
            raise BackupError(f"Stop {service} before backing up the database and documents")


def _require_quiescent(states: dict[str, str], mode: str) -> None:
    require_stopped_writers(states)
    if mode == "FIXTURE" and states.get("mock") not in ("exited", "created"):
        raise BackupError("Stop mock before backing up fixture receipts")


def _required_receipt_ids(project: str, environment: dict[str, str]) -> set[str]:
    raw = _sql(project, "SELECT COALESCE(json_agg(id)::text, '[]') FROM applications "
               "WHERE target='MOCK' AND state='CONFIRMED'",
               environment)
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError()
        return set(values)
    except (ValueError, TypeError):
        raise BackupError("Fixture receipt history could not be validated") from None


def _read_receipts(path: Path) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_RECEIPT_SNAPSHOT_BYTES:
        raise BackupError("Fixture receipt snapshot is missing, unsafe or exceeds its size limit")
    try:
        receipts = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipts, dict):
            raise ValueError()
        fields = {"application_id", "requisition", "receipt", "timestamp", "confirmation_url",
                  "confirmation_text", "reference_number", "submission_count"}
        for identity, row in receipts.items():
            if (not isinstance(identity, str) or not identity or len(identity) > 128
                    or not isinstance(row, dict) or set(row) != fields
                    or row["application_id"] != identity
                    or type(row["submission_count"]) is not int or row["submission_count"] != 1
                    or any(not isinstance(row[key], str) or not row[key] or len(row[key]) > 8192
                           for key in fields - {"submission_count"})):
                raise ValueError()
        return receipts
    except (UnicodeError, ValueError, TypeError):
        raise BackupError("Fixture receipt snapshot is invalid") from None


def require_empty_target(project: str, source_project: str, containers: list[str], volumes: list[str]) -> None:
    if not PROJECT.fullmatch(project) or project == source_project:
        raise BackupError("Restore requires a distinct, valid target project name")
    if containers or any(name.startswith(project + "_") for name in volumes):
        raise BackupError("Target project or its data volumes already exists; choose a fresh project")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_runtime_configuration(snapshot: object, operation_mode: str) -> None:
    if (operation_mode not in ("PERSONAL", "FIXTURE")
            or not isinstance(snapshot, dict)
            or set(snapshot) != {"schema_version", "operation_mode", "settings"}):
        raise BackupError("Backup runtime configuration is invalid")
    if (type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != 1
            or snapshot.get("operation_mode") != operation_mode):
        raise BackupError("Backup runtime configuration does not match its workspace mode")
    settings = snapshot.get("settings")
    if not isinstance(settings, dict) or set(settings) != RUNTIME_SETTING_NAMES:
        raise BackupError("Backup runtime configuration contains unsupported settings")
    if settings["MJOBS_OPERATION_MODE"] != operation_mode:
        raise BackupError("Backup runtime configuration does not match its workspace mode")
    expected_mock = "1" if operation_mode == "FIXTURE" else "0"
    if settings["MJOBS_COMPOSE_MOCK"] != expected_mock:
        raise BackupError("Backup runtime configuration has an invalid mock mode")
    for name, (minimum, maximum) in RUNTIME_SETTING_BOUNDS.items():
        value = settings[name]
        if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,6}", value)
                or not minimum <= int(value) <= maximum):
            raise BackupError("Backup runtime configuration contains an invalid setting")


def _runtime_configuration_snapshot(container: str, operation_mode: str) -> dict:
    """Capture only fixed, bounded non-secret settings from the API container."""
    raw = _run(["docker", "inspect", "--format", '{{json .Config.Env}}', container],
               action="Inspect API runtime configuration")
    try:
        entries = json.loads(raw.decode("utf-8"))
        if not isinstance(entries, list) or any(not isinstance(item, str) or "=" not in item for item in entries):
            raise ValueError("invalid environment shape")
        environment: dict[str, str] = {}
        for item in entries:
            name, value = item.split("=", 1)
            if name in RUNTIME_SETTING_NAMES:
                if name in environment:
                    raise ValueError("duplicate allowlisted setting")
                environment[name] = value
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise BackupError("API runtime configuration could not be safely captured") from None
    snapshot = {
        "schema_version": 1,
        "operation_mode": operation_mode,
        "settings": environment,
    }
    _validate_runtime_configuration(snapshot, operation_mode)
    return snapshot


def _read_runtime_configuration(path: Path, operation_mode: str) -> dict:
    if path.stat().st_size > MAX_RUNTIME_SNAPSHOT_BYTES:
        raise BackupError("Backup runtime configuration exceeds its size limit")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise BackupError("Backup runtime configuration is not valid JSON") from None
    _validate_runtime_configuration(snapshot, operation_mode)
    return snapshot


def _is_reparse_point(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _has_link_ancestor(path: Path) -> bool:
    # Inspect the original spelling before resolve() erases junction ancestors.
    original = path.absolute()
    return any(_is_reparse_point(part) for part in (original, *original.parents))


def verify_backup(archive: Path) -> dict:
    if _has_link_ancestor(archive):
        raise BackupError("Backup directory is a symbolic link")
    archive = archive.resolve()
    if not archive.is_dir():
        raise BackupError("Backup directory is missing or is a symbolic link")
    if _is_reparse_point(archive / "documents") or not (archive / "documents").is_dir():
        raise BackupError("Backup document directory is missing or unsafe")
    manifest_path = archive / "manifest.json"
    if _is_reparse_point(manifest_path) or not manifest_path.is_file():
        raise BackupError("Backup manifest is missing or is a symbolic link")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("Backup manifest is not valid JSON") from exc
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") not in (1, 2, 3)):
        raise BackupError("Unsupported backup manifest version")
    schema_version = manifest["schema_version"]
    if not PROJECT.fullmatch(str(manifest.get("source_project", ""))):
        raise BackupError("Backup source project is invalid")
    if manifest.get("operation_mode") not in ("PERSONAL", "FIXTURE"):
        raise BackupError("Backup operation mode is invalid")
    if not isinstance(manifest.get("alembic_revision"), str) or not manifest["alembic_revision"]:
        raise BackupError("Backup migration revision is missing")
    if not SHA256.fullmatch(str(manifest.get("compose_sha256", ""))):
        raise BackupError("Backup Compose hash is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or "database.dump" not in files:
        raise BackupError("Backup database dump is missing from manifest")
    for name, expected in files.items():
        allowed = (isinstance(name, str)
                   and (name == "database.dump" or name.startswith("documents/")
                        or (schema_version >= 2 and name in CONFIGURATION_FILES)
                        or (schema_version == 3 and name == RECEIPT_PATH)))
        if not allowed:
            raise BackupError("Backup manifest contains an invalid path")
        parts = name.split("/")
        if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
            raise BackupError("Backup manifest contains an unsafe path")
        if not SHA256.fullmatch(str(expected)):
            raise BackupError("Backup manifest contains an invalid checksum")
        path = archive.joinpath(*parts)
        if _has_link_ancestor(path) or not path.is_file() or not path.resolve().is_relative_to(archive):
            raise BackupError("Backup file is missing or unsafe: " + name)
        if checksum(path) != expected:
            raise BackupError("Backup checksum mismatch: " + name)
    actual = {
        path.relative_to(archive).as_posix()
        for path in archive.rglob("*") if path.is_file()
    }
    if actual != set(files) | {"manifest.json"}:
        raise BackupError("Backup contains unlisted or missing files")
    for path in archive.rglob("*"):
        if _is_reparse_point(path):
            raise BackupError("Backup contains a symbolic link")
        if not path.is_file() and not path.is_dir():
            raise BackupError("Backup contains an unsupported filesystem node")
    if schema_version == 3:
        if manifest["operation_mode"] != "FIXTURE" or RECEIPT_PATH not in files:
            raise BackupError("Fixture receipt snapshot is required only for FIXTURE schema 3")
        _read_receipts(archive / RECEIPT_PATH)
        if any(path.is_dir() for path in (archive / "mock").rglob("*")):
            raise BackupError("Fixture receipt snapshot contains an unsupported path")
    if schema_version >= 2:
        if not CONFIGURATION_FILES.issubset(files):
            raise BackupError("Backup configuration snapshot is incomplete")
        configuration_dir = archive / "configuration"
        if any(path.is_dir() for path in configuration_dir.rglob("*")):
            raise BackupError("Backup configuration snapshot contains an unsupported path")
        compose_path = configuration_dir / "compose.yml"
        if compose_path.stat().st_size > MAX_COMPOSE_SNAPSHOT_BYTES:
            raise BackupError("Backup Compose snapshot exceeds its size limit")
        if checksum(compose_path) != manifest["compose_sha256"]:
            raise BackupError("Archived Compose source does not match its recorded hash")
        _read_runtime_configuration(configuration_dir / "runtime.json", manifest["operation_mode"])
    return manifest


def _run(command: list[str], *, environment: dict[str, str] | None = None,
         stdin=None, stdout=None, timeout: int = 120, action: str) -> bytes:
    capture_stdout = stdout is None
    captured = []
    captured_size = 0
    output_limit_exceeded = threading.Event()

    def drain(stream, *, retain_limit=None):
        nonlocal captured_size
        try:
            while True:
                chunk = stream.read(_OUTPUT_CHUNK_BYTES)
                if not chunk:
                    return
                if retain_limit is None:
                    continue
                available = max(0, retain_limit - captured_size)
                if available:
                    captured.append(chunk[:available])
                    captured_size += min(len(chunk), available)
                if len(chunk) > available:
                    output_limit_exceeded.set()
        except (OSError, ValueError):
            return

    def stop_child(process):
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()

    try:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=stdin,
                                   stdout=subprocess.PIPE if capture_stdout else stdout,
                                   stderr=subprocess.PIPE, shell=False)
    except OSError as exc:
        raise BackupError(f"{action} could not complete; inspect Docker state") from exc
    readers = [threading.Thread(target=drain, args=(process.stderr,), daemon=True)]
    if capture_stdout:
        readers.append(threading.Thread(target=drain,
            kwargs={"stream": process.stdout, "retain_limit": MAX_CAPTURED_STDOUT_BYTES}, daemon=True))
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if output_limit_exceeded.is_set():
                stop_child(process)
                raise BackupError(f"{action} exceeded the captured-output limit")
            if time.monotonic() >= deadline:
                stop_child(process)
                raise BackupError(f"{action} could not complete; inspect Docker state")
            time.sleep(0.01)
        returncode = process.wait()
        for reader in readers:
            reader.join(timeout=1)
        if output_limit_exceeded.is_set():
            raise BackupError(f"{action} exceeded the captured-output limit")
        if returncode:
            raise BackupError(f"{action} failed with exit code {returncode}; inspect Docker logs")
        return b"".join(captured) if capture_stdout else b""
    finally:
        if process.poll() is None:
            stop_child(process)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        for reader in readers:
            reader.join(timeout=1)


def _compose(project: str, *arguments: str, environment: dict[str, str] | None = None,
             stdin=None, stdout=None, timeout: int = 120, action: str) -> bytes:
    return _run(["docker", "compose", "-p", project, *arguments], environment=environment,
                stdin=stdin, stdout=stdout, timeout=timeout, action=action)


def _config(mode: str | None = None, port: int | None = None) -> dict[str, str]:
    if not (ROOT / ".env").is_file():
        raise BackupError("Project-local .env is missing; run python -m cli.init_env first")
    environment = os.environ.copy()
    if mode:
        environment["MJOBS_OPERATION_MODE"] = mode
        environment["MJOBS_COMPOSE_MOCK"] = "1" if mode == "FIXTURE" else "0"
        environment["COMPOSE_PROFILES"] = "fixture" if mode == "FIXTURE" else ""
    if port:
        environment["MJOBS_PORT"] = str(port)
    return environment


def _service_states(project: str, environment: dict[str, str]) -> dict[str, str]:
    raw = _compose(project, "ps", "-a", "--format", "json", environment=environment,
                   action="Inspect project services").decode("utf-8")
    states: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        service, state = row["Service"], row["State"]
        if service not in states or state == "running":
            states[service] = state
    return states


def _container(project: str, service: str, environment: dict[str, str]) -> str:
    identifier = _compose(project, "ps", "-a", "-q", service, environment=environment,
                          action=f"Locate {service} container").decode("utf-8").strip()
    if not identifier or "\n" in identifier:
        raise BackupError(f"Exactly one {service} container is required")
    label = _run(["docker", "inspect", "--format", '{{ index .Config.Labels "com.docker.compose.project" }}',
                  identifier], action="Check container ownership").decode("utf-8").strip()
    if label != project:
        raise BackupError("Container belongs to another project")
    return identifier


def _sql(project: str, query: str, environment: dict[str, str]) -> str:
    result = _compose(project, "exec", "-T", "postgres", "psql", "-U", "mjobs", "-d", "mjobs",
                      "-AtX", "-c", query, environment=environment,
                      action="Read database backup metadata").decode("utf-8").strip()
    if not result or "\n" in result:
        raise BackupError("Database backup metadata is missing or ambiguous")
    return result


def create_backup(project: str, archive: Path) -> dict:
    if not PROJECT.fullmatch(project):
        raise BackupError("Invalid source project name")
    if _has_link_ancestor(archive):
        raise BackupError("Backup destination contains an unsafe link")
    if archive.exists() or archive.is_symlink():
        raise BackupError("Backup destination already exists; choose a new path")
    parent = archive.absolute().parent.resolve()
    if not parent.is_dir():
        raise BackupError("Backup parent directory does not exist")
    environment = _config()
    states = _service_states(project, environment)
    require_stopped_writers(states)
    container = _container(project, "api", environment)
    mode = _sql(project, "SELECT operation_mode FROM workspace_identity WHERE id='workspace'", environment)
    if mode not in ("PERSONAL", "FIXTURE"):
        raise BackupError("Database operation mode is not bound")
    revision = _sql(project, "SELECT version_num FROM alembic_version", environment)
    _require_quiescent(states, mode)
    stage = Path(tempfile.mkdtemp(prefix=".mjobs-backup-", dir=parent))
    try:
        with (stage / "database.dump").open("wb") as stream:
            _compose(project, "exec", "-T", "postgres", "pg_dump", "-U", "mjobs", "-d", "mjobs",
                     "-Fc", "--no-owner", "--no-acl", environment=environment,
                     stdout=stream, timeout=600, action="Dump PostgreSQL")
        _require_quiescent(_service_states(project, environment), mode)
        documents = stage / "documents"
        documents.mkdir()
        _run(["docker", "cp", container + ":/data/documents/.", str(documents)],
             action="Copy project documents", timeout=600)
        _require_quiescent(_service_states(project, environment), mode)
        # Playwright browser profiles may contain live cookies. They are not
        # documents and must not be carried into a portable backup.
        for directory, children, _ in os.walk(documents, topdown=True, followlinks=False):
            for child in list(children):
                if _is_reparse_point(Path(directory) / child):
                    raise BackupError("Document volume contains an unsupported file type")
                if child != "browser-profile":
                    continue
                target = Path(directory) / child
                if not target.resolve().is_relative_to(documents.resolve()):
                    raise BackupError("Unsafe browser-profile path in document volume")
                shutil.rmtree(target)
                children.remove(child)
        files: dict[str, str] = {"database.dump": checksum(stage / "database.dump")}
        for path in sorted(documents.rglob("*")):
            if _is_reparse_point(path) or (not path.is_file() and not path.is_dir()):
                raise BackupError("Document volume contains an unsupported file type")
            if path.is_file():
                files[path.relative_to(stage).as_posix()] = checksum(path)
        compose_source = ROOT / "compose.yml"
        if not compose_source.is_file() or compose_source.is_symlink():
            raise BackupError("Public Compose source is missing or unsafe")
        if compose_source.stat().st_size > MAX_COMPOSE_SNAPSHOT_BYTES:
            raise BackupError("Public Compose source exceeds the snapshot size limit")
        configuration = stage / "configuration"
        configuration.mkdir()
        shutil.copyfile(compose_source, configuration / "compose.yml")
        runtime_configuration = _runtime_configuration_snapshot(container, mode)
        (configuration / "runtime.json").write_text(
            json.dumps(runtime_configuration, indent=2) + "\n", encoding="utf-8")
        for name in sorted(CONFIGURATION_FILES):
            files[name] = checksum(stage.joinpath(*name.split("/")))
        if mode == "FIXTURE":
            required = _required_receipt_ids(project, environment)
            receipt_directory = stage / "mock"
            receipt_directory.mkdir()
            mock = _container(project, "mock", environment)
            _run(["docker", "cp", mock + ":/data/.", str(receipt_directory)],
                 action="Copy fixture provider receipts", timeout=120)
            # Docker seeds /data from the image, including its empty document
            # directory. It is not provider state and must not enter the archive.
            image_documents = receipt_directory / "documents"
            if image_documents.is_dir() and not image_documents.is_symlink() and not any(image_documents.iterdir()):
                image_documents.rmdir()
            if any(path.name != "receipts.json" or not path.is_file() or path.is_symlink()
                   for path in receipt_directory.iterdir()):
                raise BackupError("Fixture receipt volume contains an unsupported file")
            receipt_path = stage / RECEIPT_PATH
            if not receipt_path.exists() and not required:
                receipt_path.write_text("{}\n", encoding="utf-8")
            receipts = _read_receipts(receipt_path)
            if not required.issubset(receipts):
                raise BackupError("Fixture receipt snapshot is missing application history; preserve source for recovery")
            files[RECEIPT_PATH] = checksum(receipt_path)
        manifest = {
            "schema_version": 3 if mode == "FIXTURE" else 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_project": project,
            "operation_mode": mode,
            "alembic_revision": revision,
            "compose_sha256": checksum(compose_source),
            "files": files,
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        verify_backup(stage)
        _require_quiescent(_service_states(project, environment), mode)
        if archive.exists() or archive.is_symlink():
            raise BackupError("Backup destination appeared during capture; refusing overwrite")
        os.rename(stage, archive)
        return manifest
    finally:
        if stage.exists():
            if stage.resolve().parent != parent or not stage.name.startswith(".mjobs-backup-"):
                raise BackupError("Refusing to remove an unexpected staging directory")
            shutil.rmtree(stage)


def _volumes() -> list[str]:
    return _run(["docker", "volume", "ls", "--format", "{{.Name}}"],
                action="Inspect Docker volumes").decode("utf-8").splitlines()


def _wait_ready(port: int) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readiness", timeout=5) as response:
                status = json.load(response)
            if status.get("status") == "ready":
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/settings", timeout=5) as response:
                    settings = json.load(response)
                if settings.get("external_submission_enabled") is False:
                    return
                raise BackupError("Restored application did not keep external submission disabled")
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(1)
    raise BackupError("Restored project did not become ready within 120 seconds")


def verify_credential_recoverability(database_url: str) -> int:
    """Require the configured key to decrypt every saved credential without returning values."""
    if not isinstance(database_url, str) or not database_url:
        raise BackupError("Restored database configuration is unavailable")
    store = Store(database_url)
    try:
        cipher = CredentialCipher.from_environment()
        with store.engine.connect() as connection:
            rows = connection.execute(
                models.provider_credentials.select().with_only_columns(
                    models.provider_credentials.c.profile_id,
                    models.provider_credentials.c.provider,
                    models.provider_credentials.c.encrypted_secret,
                )
            )
            count = 0
            for profile_id, provider, encrypted in rows:
                cipher.decrypt(profile_id, provider, encrypted)
                count += 1
        return count
    except CredentialConfigurationError:
        raise BackupError(
            "Saved provider credentials could not be verified; use the original credential-encryption key"
        ) from None
    finally:
        store.engine.dispose()


def restore_backup(archive: Path, project: str, port: int) -> dict:
    """Pin verified bytes before Docker calls can race source-archive edits."""
    manifest = verify_backup(archive)
    archive = archive.resolve()
    # Restore is a multi-step operation. Rechecking the caller's archive
    # before each Docker call would still let a changed file reach pg_restore
    # or docker cp. Copy only the verified inventory into an owned snapshot,
    # verify the copied bytes, and use that tree for the entire operation.
    with tempfile.TemporaryDirectory(prefix="mjobs-restore-snapshot-") as directory:
        snapshot = Path(directory)
        try:
            (snapshot / "documents").mkdir()
            for name, expected in manifest["files"].items():
                source = archive.joinpath(*name.split("/"))
                if (not source.is_file() or source.is_symlink()
                        or not source.resolve().is_relative_to(archive)):
                    raise BackupError("Backup file changed before snapshot")
                current = source
                while current != archive:
                    if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                        raise BackupError("Backup path changed before snapshot")
                    current = current.parent
                destination = snapshot.joinpath(*name.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination, follow_symlinks=False)
                if destination.is_symlink() or checksum(destination) != expected:
                    raise BackupError("Backup checksum mismatch while preparing restore snapshot")
            # Pin validated metadata as well; never reread a replaced manifest.
            (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            if verify_backup(snapshot) != manifest:
                raise BackupError("Backup metadata changed while preparing restore snapshot")
        except OSError:
            raise BackupError("Restore snapshot could not be prepared") from None
        return _restore_verified_snapshot(snapshot, manifest, project, port)


def _restore_verified_snapshot(archive: Path, manifest: dict, project: str, port: int) -> dict:
    if checksum(ROOT / "compose.yml") != manifest["compose_sha256"]:
        raise BackupError("Compose configuration differs from the backup; use matching code")
    if not 1024 <= port <= 65535:
        raise BackupError("Restore port must be between 1024 and 65535")
    environment = _config(manifest["operation_mode"], port)
    containers = _compose(project, "ps", "-a", "-q", environment=environment,
                          action="Inspect restore target").decode("utf-8").splitlines()
    require_empty_target(project, manifest["source_project"], containers, _volumes())
    _run(["docker", "image", "inspect", "mjobs-local:spine"], action="Check local application image")
    _compose(project, "up", "-d", "--wait", "--wait-timeout", "120", "postgres", environment=environment,
             timeout=180, action="Start fresh PostgreSQL")
    if _sql(project, "SELECT count(*) FROM pg_tables WHERE schemaname='public'", environment) != "0":
        raise BackupError("Fresh target database unexpectedly contains tables; refusing restore")
    with (archive / "database.dump").open("rb") as stream:
        _compose(project, "exec", "-T", "postgres", "pg_restore", "-U", "mjobs", "-d", "mjobs",
                 "--clean", "--if-exists", "--no-owner", "--no-acl", "--exit-on-error",
                 "--single-transaction", environment=environment, stdin=stream,
                 timeout=600, action="Restore PostgreSQL")
    if _sql(project, "SELECT version_num FROM alembic_version", environment) != manifest["alembic_revision"]:
        raise BackupError("Restored migration revision differs from the backup")
    if _sql(project, "SELECT operation_mode FROM workspace_identity WHERE id='workspace'", environment) != manifest["operation_mode"]:
        raise BackupError("Restored workspace mode differs from the backup")
    if manifest["operation_mode"] == "FIXTURE":
        required = _required_receipt_ids(project, environment)
        receipts = _read_receipts(archive / RECEIPT_PATH) if manifest["schema_version"] == 3 else {}
        if not required.issubset(receipts):
            raise BackupError("Backup lacks fixture receipt history; application remains stopped for recovery")
    _compose(project, "run", "--rm", "--no-deps", "-T", "migrate", "python", "-m",
             "cli.provision_database", "--provision-only", environment=environment,
             timeout=120, action="Provision restored runtime database login")
    _compose(project, "run", "--rm", "--no-deps", "-T", "api", "python", "-m",
             "cli.local_backup", "verify-credentials", environment=environment,
             timeout=120, action="Verify restored credential encryption key")
    _compose(project, "create", "api", environment=environment,
             action="Create fresh document volume")
    container = _container(project, "api", environment)
    _run(["docker", "cp", str(archive / "documents") + os.sep + ".", container + ":/data/documents/"],
         timeout=600, action="Restore project documents")
    _compose(project, "run", "--rm", "--no-deps", "--user", "root", "api", "chown", "-R",
             "10001:10001", "/data/documents", environment=environment,
             timeout=600, action="Restore document ownership")
    with (archive / "manifest.json").open("rb") as stream:
        _compose(project, "run", "--rm", "--no-deps", "-T", "api", "python", "-m",
                 "cli.local_backup", "verify-documents", environment=environment,
                 stdin=stream, timeout=600, action="Verify restored documents")
    if manifest["operation_mode"] == "FIXTURE" and manifest["schema_version"] == 3:
        _compose(project, "create", "mock", environment=environment,
                 action="Create fresh fixture receipt volume")
        mock = _container(project, "mock", environment)
        _run(["docker", "cp", str(archive / RECEIPT_PATH), mock + ":/data/receipts.json"],
             timeout=120, action="Restore fixture provider receipts")
        _compose(project, "run", "--rm", "--no-deps", "--user", "root", "mock", "chown",
                 "10001:10001", "/data/receipts.json", environment=environment,
                 action="Restore fixture receipt ownership")
    _compose(project, "up", "-d", "--no-build", "--wait", "--wait-timeout", "180",
             environment=environment, timeout=240, action="Start restored application")
    _wait_ready(port)
    return manifest


def verify_document_tree(manifest: dict, root: Path) -> None:
    """Confirm that the restored volume has exactly the archived document files."""
    documents = root / "documents"
    if _has_link_ancestor(documents) or not documents.is_dir():
        raise BackupError("Restored document directory is missing or unsafe")
    expected_files = {name for name in manifest["files"] if name.startswith("documents/")}
    actual_files: set[str] = set()
    for path in documents.rglob("*"):
        if _is_reparse_point(path) or (not path.is_file() and not path.is_dir()):
            raise BackupError("Restored document volume contains an unsafe file")
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
    if actual_files != expected_files:
        raise BackupError("Restored document volume contains unlisted or missing files")
    for name, expected in manifest["files"].items():
        if not name.startswith("documents/"):
            continue
        parts = name.split("/")
        if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
            raise BackupError("Unsafe document path")
        path = root.joinpath(*parts)
        if not path.is_file() or path.is_symlink() or checksum(path) != expected:
            raise BackupError("Restored document checksum mismatch")


def verify_documents_from_stdin() -> None:
    """Run in the application image against its mounted document volume."""
    manifest = json.load(sys.stdin)
    verify_document_tree(manifest, Path("/data"))
    print("Restored documents verified")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--project", required=True)
    backup.add_argument("--archive", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--target-project", required=True)
    restore.add_argument("--port", type=int, required=True)
    commands.add_parser("verify-credentials", help=argparse.SUPPRESS)
    commands.add_parser("verify-documents", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            manifest = verify_backup(args.archive)
            print(f"Backup verified: {len(manifest['files'])} files; mode {manifest['operation_mode']}")
            if manifest["schema_version"] == 2:
                print("Configuration snapshot: configuration/runtime.json (manual review only; settings are not applied)")
        elif args.command == "backup":
            manifest = create_backup(args.project, args.archive)
            print(f"Backup created: {len(manifest['files'])} files; mode {manifest['operation_mode']}")
            print("Configuration snapshot: non-secret runtime settings saved for manual review")
        elif args.command == "restore":
            manifest = restore_backup(args.archive, args.target_project, args.port)
            print(f"Restored {manifest['operation_mode']} project {args.target_project} at 127.0.0.1:{args.port}")
            if manifest["schema_version"] == 2:
                print("Configuration snapshot: configuration/runtime.json (manual review only; settings are not applied)")
        elif args.command == "verify-credentials":
            count = verify_credential_recoverability(os.environ.get("MJOBS_DATABASE_URL"))
            print(f"Saved provider credentials verified: {count}")
        elif args.command == "verify-documents":
            verify_documents_from_stdin()
    except BackupError as exc:
        if args.command == "restore":
            parser.exit(2, f"Restore failed: {exc}. A new target may be partial; preserve it for diagnosis.\n")
        if args.command == "verify-credentials":
            parser.exit(2, f"Credential verification failed: {exc}\n")
        parser.exit(2, f"Backup refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
