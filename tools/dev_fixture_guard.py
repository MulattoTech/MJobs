"""Bounded, disposable MJobs Compose fixtures; never targets production stacks.

Only projects created by this tool carry its ownership label and are eligible for
cleanup. Unlabelled legacy fixtures, personal stacks and production are untouched.
"""
import argparse
import base64
from collections import defaultdict
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yml"
OVERRIDE = ROOT / "compose.dev-fixture.yml"
RECOVERY_OVERRIDE = ROOT / "compose.dev-fixture-recovery.yml"
PATTERN = re.compile(r"mjobs-fixture-[0-9a-f]{12}\Z")
TOKEN_LABEL = "com.mjobs.dev.guard"
EXPIRY_LABEL = "com.mjobs.dev.expires"
MODE_LABEL = "com.mjobs.dev.guard.mode"
RECOVERY_MODE = "recovery"
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE_REF_PATTERN = re.compile(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
DOCKER_CONTEXT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
RECOVERY_ENV_ROOT = Path(tempfile.gettempdir()) / "mjobs-dev-fixture-guard"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
SERVICES = frozenset({
    "postgres", "redis", "migrate", "api", "mock", "worker", "scheduler",
    "source-worker", "company-discovery-worker", "ai-worker", "document-worker",
})
VOLUMES = frozenset({
    "database", "queue", "documents", "mock-receipts",
    "runtime-database", "migration-backups",
})
NETWORKS = frozenset({"private", "ui", "source-egress"})
DOCKER_ENV_KEYS = (
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC",
    "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMW6432",
    "DOCKER_CONFIG",
)
RECOVERY_ENV_VALUES = {
    "MJOBS_COMPOSE_MOCK": "1",
    "MJOBS_OPERATION_MODE": "FIXTURE",
    "MJOBS_EXTERNAL_SUBMISSIONS_ENABLED": "0",
    "MJOBS_EXTERNAL_SUBMISSION_ENABLED": "0",
    "MJOBS_PUBLIC_HOST": "",
    "MJOBS_DEPLOY_SHA": "",
    "MJOBS_NOTIFICATIONS_ENABLED": "0",
    "MJOBS_OLLAMA_BASE_URL": "http://127.0.0.1:9",
    "MJOBS_OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    "MJOBS_OPENAI_API_KEY": "",
    "MJOBS_OPENAI_COMPATIBLE_BASE_URL": "http://127.0.0.1:9/v1",
    "MJOBS_OPENAI_COMPATIBLE_API_KEY": "",
    "MJOBS_LOCAL_AI_BASE_URL": "http://127.0.0.1:9/v1",
    "MJOBS_LOCAL_AI_API_KEY": "",
    "MJOBS_AZURE_OPENAI_BASE_URL": "http://127.0.0.1:9",
    "MJOBS_AZURE_OPENAI_API_KEY": "",
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    "AZURE_OPENAI_API_KEY": "",
    "AZURE_OPENAI_ENDPOINT": "http://127.0.0.1:9",
}


def recovery_synthetic_credentials(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Invalid disposable fixture token")
    key = bytes.fromhex(token)
    password = hmac.new(key, b"mjobs-fixture-postgres", hashlib.sha256).digest()[:24]
    vault_key = hmac.new(key, b"mjobs-fixture-vault", hashlib.sha256).digest()
    return {
        "POSTGRES_PASSWORD": base64.urlsafe_b64encode(password).decode("ascii").rstrip("="),
        "MJOBS_CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(vault_key).decode("ascii"),
    }


def docker(*args, env=None, timeout=90):
    try:
        result = subprocess.run(
            ["docker", *args], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Docker command could not complete") from exc
    if result.returncode:
        raise RuntimeError(
            f"Docker command failed ({result.returncode}); stderr omitted"
        )
    return result.stdout.strip()


def containers(project=None, running=False, env=None):
    filters = ["--filter", f"label={PROJECT_LABEL}"]
    if project is not None:
        filters = ["--filter", f"label={PROJECT_LABEL}={project}"]
    ids = docker("ps", "-q" if running else "-aq", *filters, env=env).split()
    if not ids:
        return []
    lines = docker("inspect", "--format", "{{json .Config.Labels}}", *ids, env=env).splitlines()
    if len(lines) != len(ids):
        raise RuntimeError("Container inspection count mismatch")
    labels = [json.loads(line) for line in lines]
    if any(not isinstance(row, dict) for row in labels):
        raise RuntimeError("Container labels were unavailable")
    return labels


def project_resources(project, env=None):
    return {
        "containers": containers(project, env=env),
        "volumes": docker("volume", "ls", "-q", "--filter",
                          f"label={PROJECT_LABEL}={project}", env=env).splitlines(),
        "networks": docker("network", "ls", "-q", "--filter",
                           f"label={PROJECT_LABEL}={project}", env=env).splitlines(),
    }


def compose_files(recovery_mode=False):
    """Return the only supported ordered Compose file list for each mode."""
    files = (COMPOSE, OVERRIDE, RECOVERY_OVERRIDE) if recovery_mode else (COMPOSE, OVERRIDE)
    return tuple(str(path.resolve()) for path in files)


def inspect_resources(kind, project=None, recovery_mode=False, include_named=False, env=None):
    if kind not in ("volume", "network"):
        raise ValueError("Unsupported fixture resource type")
    filters = ["--filter", f"label={PROJECT_LABEL}"]
    if project is not None:
        filters = ["--filter", f"label={PROJECT_LABEL}={project}"]
    if recovery_mode:
        filters.extend(["--filter", f"label={MODE_LABEL}={RECOVERY_MODE}"])
    ids = docker(kind, "ls", "-q", *filters, env=env).split()
    if project is not None and include_named:
        named = docker(kind, "ls", "-q", "--filter", f"name={project}_", env=env).split()
        ids = list(dict.fromkeys([*ids, *named]))
    if not ids:
        return []
    lines = docker(kind, "inspect", "--format", "{{json .}}", *ids, env=env).splitlines()
    if len(lines) != len(ids):
        raise RuntimeError(f"{kind} inspection count mismatch")
    rows = [json.loads(line) for line in lines]
    if any(not isinstance(row, dict) or not isinstance(row.get("Labels"), dict)
           for row in rows):
        raise RuntimeError(f"{kind} labels were unavailable")
    return rows


def recovery_env_path(project):
    if not PATTERN.fullmatch(project):
        raise ValueError("Not an isolated MJobs fixture project")
    return RECOVERY_ENV_ROOT / f"{project}.env"


def validate_recovery_dotenv_values(project, token, expiry, values):
    expected = {
        "MJOBS_DEV_GUARD_TOKEN": token,
        "MJOBS_DEV_GUARD_EXPIRES": str(expiry),
        **recovery_synthetic_credentials(token),
    }
    image_ref = values.get("MJOBS_RECOVERY_IMAGE_REF", "")
    image_id = values.get("MJOBS_RECOVERY_IMAGE_ID", "")
    context = values.get("MJOBS_RECOVERY_DOCKER_CONTEXT", "")
    endpoint = values.get("MJOBS_RECOVERY_DOCKER_ENDPOINT", "")
    if (set(values) != set(recovery_dotenv_values({}))
            or any(values.get(key) != value for key, value in expected.items())
            or any(values.get(key) != value for key, value in RECOVERY_ENV_VALUES.items())
            or not IMAGE_REF_PATTERN.fullmatch(image_ref)
            or not IMAGE_ID_PATTERN.fullmatch(image_id)
            or values.get("MJOBS_IMAGE") != image_id
            or not DOCKER_CONTEXT_PATTERN.fullmatch(context)
            or normalize_local_docker_endpoint(endpoint) != endpoint):
        raise RuntimeError("Refusing recovery: controlled dotenv identity is invalid")


def recovery_environment_file(project, token, expiry, environment=None):
    """Create or verify the private synthetic dotenv shared by recovery lifecycle commands."""
    path = recovery_env_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Refusing cleanup: recovery dotenv is not a regular file")
    if path.exists():
        values = read_recovery_dotenv(path)
        validate_recovery_dotenv_values(project, token, expiry, values)
        return path

    if environment is None:
        raise RuntimeError("Refusing recovery: controlled dotenv is missing")
    values = recovery_dotenv_values(environment)
    validate_recovery_dotenv_values(project, token, expiry, values)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents a stale or foreign file from being replaced.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for key, value in sorted(values.items()):
                if "\n" in value or "\r" in value:
                    raise ValueError("Invalid recovery dotenv value")
                stream.write(f"{key}={value}\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def read_recovery_dotenv(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise RuntimeError("Refusing cleanup: invalid recovery dotenv")
        values[key] = value
    return values


def controlled_docker_environment(environment=None, context_name=None):
    source = os.environ if environment is None else environment
    result = {key: str(source[key]) for key in DOCKER_ENV_KEYS if source.get(key)}
    if context_name is not None:
        if not DOCKER_CONTEXT_PATTERN.fullmatch(context_name):
            raise ValueError("Invalid local Docker context name")
        result["DOCKER_CONTEXT"] = context_name
    return result


def normalize_local_docker_endpoint(endpoint):
    if not isinstance(endpoint, str) or not endpoint or endpoint.strip() != endpoint:
        raise ValueError("Docker endpoint is missing or ambiguous")
    normalized = endpoint.replace("\\", "/").lower()
    if re.fullmatch(r"npipe:/{2,4}\./pipe/[a-z0-9._-]+", normalized):
        return normalized
    if re.fullmatch(r"unix:///var/run/docker\.sock", normalized) or re.fullmatch(
            r"unix:///run/user/[0-9]+/docker\.sock", normalized):
        return normalized
    if normalized.startswith("tcp://"):
        try:
            from urllib.parse import urlsplit
            parsed = urlsplit(normalized)
            host = parsed.hostname or ""
            port = parsed.port
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            local, port = False, None
        if (local and port is not None and parsed.username is None and parsed.password is None
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
            return f"tcp://{parsed.hostname.lower()}:{port}"
    raise ValueError("Docker context must resolve to an unambiguous local engine endpoint")


def validate_local_docker_context(context_name, expected_endpoint=None, environment=None):
    if not isinstance(context_name, str) or not DOCKER_CONTEXT_PATTERN.fullmatch(context_name):
        raise ValueError("Recovery mode requires an explicit Docker context")
    docker_env = controlled_docker_environment(environment, context_name=context_name)
    selected = docker("context", "show", env=docker_env).strip()
    if selected != context_name:
        raise RuntimeError("Docker context selection changed during recovery")
    endpoint = docker("context", "inspect", context_name, "--format",
                      "{{.Endpoints.docker.Host}}", env=docker_env).strip()
    normalized = normalize_local_docker_endpoint(endpoint)
    if expected_endpoint is not None:
        if normalize_local_docker_endpoint(expected_endpoint) != normalized:
            raise RuntimeError("Docker context endpoint changed during recovery")
    return context_name, normalized, docker_env


def validate_local_campaign_image(image_ref, image_id, environment, check_tag=True):
    if (not isinstance(image_ref, str) or not IMAGE_REF_PATTERN.fullmatch(image_ref)
            or not isinstance(image_id, str) or not IMAGE_ID_PATTERN.fullmatch(image_id)):
        raise ValueError("Recovery mode requires an explicit tagged local image and immutable image ID")
    resolved_id = docker("image", "inspect", "--format", "{{.Id}}", image_id,
                         env=environment).strip()
    if resolved_id != image_id:
        raise RuntimeError("Selected campaign image ID is not present locally")
    if check_tag:
        tagged_id = docker("image", "inspect", "--format", "{{.Id}}", image_ref,
                           env=environment).strip()
        if tagged_id != image_id:
            raise RuntimeError("Selected local campaign image tag does not match its immutable ID")
    return image_id


def recovery_dotenv_values(environment):
    keys = {
        "MJOBS_DEV_GUARD_TOKEN", "MJOBS_DEV_GUARD_EXPIRES",
        "POSTGRES_PASSWORD", "MJOBS_CREDENTIAL_ENCRYPTION_KEY",
        "MJOBS_PORT", "MJOBS_IMAGE", "MJOBS_BROWSER_PROFILE",
        "MJOBS_WORKER_LOG_LEVEL", "MJOBS_SUBMISSION_HOURLY_LIMIT",
        "MJOBS_SUBMISSION_DAILY_LIMIT", "MJOBS_SUBMISSION_COMPANY_LIMIT",
        "MJOBS_SOURCE_COLLECTION_CONCURRENCY", "MJOBS_COMPANY_DISCOVERY_CONCURRENCY",
        "MJOBS_AI_CONCURRENCY", "MJOBS_DOCUMENT_GENERATION_CONCURRENCY",
        "MJOBS_APPLICATION_AUTOMATION_CONCURRENCY", "MJOBS_TRANSIENT_RETENTION_CONFIG",
        "MJOBS_TRANSIENT_RETENTION_INTERVAL_SECONDS", "MJOBS_MOCK_SCENARIO",
        "MJOBS_RECOVERY_IMAGE_REF", "MJOBS_RECOVERY_IMAGE_ID",
        "MJOBS_RECOVERY_DOCKER_CONTEXT", "MJOBS_RECOVERY_DOCKER_ENDPOINT",
        *RECOVERY_ENV_VALUES.keys(),
    }
    return {key: str(environment.get(key, "")) for key in keys}


def verify_owned(project, token, recovery_mode=False, env=None):
    if not PATTERN.fullmatch(project) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Invalid disposable project identity")
    rows = containers(project, env=env)
    if not rows and not recovery_mode:
        raise RuntimeError("No labelled containers to establish fixture ownership")
    expected_mode = RECOVERY_MODE if recovery_mode else None
    for row in rows:
        if (row.get(PROJECT_LABEL) != project or row.get(TOKEN_LABEL) != token
                or row.get(SERVICE_LABEL) not in SERVICES
                or row.get(MODE_LABEL) != expected_mode):
            raise RuntimeError("Refusing cleanup: project contains an unowned container")
    expiries = {row.get(EXPIRY_LABEL) for row in rows}
    # Compose-created volumes and networks are scoped to the exact generated
    # project. Refuse additional resources that might belong to another operator.
    if recovery_mode:
        resources = [("volume", row) for row in
                     inspect_resources("volume", project, include_named=True, env=env)]
        resources.extend(("network", row) for row in
                         inspect_resources("network", project, include_named=True, env=env))
        if not rows and not resources:
            # A failed compose up may have created nothing; there is nothing to reap.
            return False
        expected_labels = {
            PROJECT_LABEL: project, TOKEN_LABEL: token,
            MODE_LABEL: RECOVERY_MODE,
        }
        for kind, row in resources:
            labels = row["Labels"]
            expected_names = VOLUMES if kind == "volume" else NETWORKS
            if (row.get("Name") not in {f"{project}_{part}" for part in expected_names}
                    or any(labels.get(key) != value for key, value in expected_labels.items())
                    or not labels.get(EXPIRY_LABEL)):
                raise RuntimeError("Refusing cleanup: recovery resource ownership mismatch")
            expiries.add(labels[EXPIRY_LABEL])
        if not resources and not rows:
            return False
        if (len(expiries) != 1 or not next(iter(expiries))
                or any(row.get(EXPIRY_LABEL) != next(iter(expiries)) for row in rows)
                or any(row["Labels"].get(EXPIRY_LABEL) != next(iter(expiries))
                       for _, row in resources)):
            raise RuntimeError("Refusing cleanup: ambiguous fixture lease")
        return next(iter(expiries))

    if len(expiries) != 1 or not next(iter(expiries)):
        raise RuntimeError("Refusing cleanup: ambiguous fixture lease")
    names = docker("volume", "ls", "-q", "--filter",
                   f"label={PROJECT_LABEL}={project}", env=env).splitlines()
    if any(name not in {f"{project}_{part}" for part in VOLUMES} for name in names):
        raise RuntimeError("Refusing cleanup: unexpected project volume")
    network_ids = docker("network", "ls", "-q", "--filter",
                         f"label={PROJECT_LABEL}={project}", env=env).split()
    if network_ids:
        network_names = docker("network", "inspect", "--format",
                               "{{.Name}}", *network_ids, env=env).splitlines()
        if any(name not in {f"{project}_{part}" for part in NETWORKS}
               for name in network_names):
            raise RuntimeError("Refusing cleanup: unexpected project network")
    return True


def compose_args(project, recovery_mode=False):
    if not PATTERN.fullmatch(project):
        raise ValueError("Not an isolated MJobs fixture project")
    files = compose_files(recovery_mode)
    file_args = tuple(part for path in files for part in ("--file", path))
    env_args = ("--env-file", str(recovery_env_path(project))) if recovery_mode else ()
    return ("compose", *env_args, *file_args, "--project-directory", str(ROOT),
            "--project-name", project, "--profile", "fixture")


def cleanup(project, token, env=None, recovery_mode=False):
    env_file = None
    if recovery_mode:
        env_file = recovery_env_path(project)
        if env_file.is_symlink() or not env_file.is_file():
            raise RuntimeError("Refusing recovery cleanup: controlled dotenv is missing or unsafe")
        values = read_recovery_dotenv(env_file)
        expiry = values.get("MJOBS_DEV_GUARD_EXPIRES")
        recovery_environment_file(project, token, expiry)
        context_name = values["MJOBS_RECOVERY_DOCKER_CONTEXT"]
        expected_endpoint = values["MJOBS_RECOVERY_DOCKER_ENDPOINT"]
        if env is not None and env.get("DOCKER_CONTEXT") != context_name:
            raise RuntimeError("Refusing recovery cleanup: caller engine differs from fixture engine")
        _, _, docker_env = validate_local_docker_context(
            context_name, expected_endpoint=expected_endpoint, environment=env)
        validate_local_campaign_image(values["MJOBS_RECOVERY_IMAGE_REF"],
                                      values["MJOBS_RECOVERY_IMAGE_ID"],
                                      docker_env, check_tag=False)
        owned = verify_owned(project, token, recovery_mode=True, env=docker_env)
        if not owned:
            if env is not None:
                env_file.unlink(missing_ok=True)
            return
    else:
        owned = verify_owned(project, token, recovery_mode=False, env=env)
    # Compose interpolates its config even for "down". Supply non-production,
    # cleanup-only placeholders if the independent watchdog has no parent env.
    # Never read or reuse production secrets when reaping an expired fixture.
    rows = containers(project, env=docker_env if recovery_mode else env)
    if recovery_mode:
        expiry = str(owned)
    else:
        expiries = {row.get(EXPIRY_LABEL) for row in rows}
        if len(expiries) != 1 or not next(iter(expiries)):
            raise RuntimeError("Refusing cleanup: ambiguous fixture lease")
        expiry = next(iter(expiries))
    compose_env = dict(os.environ if env is None else env)
    if recovery_mode:
        compose_env = dict(docker_env)
        compose_env.update(values)
        compose_env["COMPOSE_ENV_FILES"] = str(env_file)
    compose_env.update({
        "MJOBS_DEV_GUARD_TOKEN": token,
        "MJOBS_DEV_GUARD_EXPIRES": expiry,
    })
    if env is None and not recovery_mode:
        compose_env["POSTGRES_PASSWORD"] = "fixture-cleanup-only"
        compose_env["MJOBS_CREDENTIAL_ENCRYPTION_KEY"] = "fixture-cleanup-only"
    compose_env.pop("COMPOSE_FILE", None)
    # Only newly generated, token-labelled projects reach this call. Preserve
    # images/build cache; delete the disposable project's own test volumes.
    if recovery_mode:
        compose_env["MJOBS_DEV_GUARD_TOKEN"] = token
        compose_env["MJOBS_DEV_GUARD_EXPIRES"] = expiry
    docker(*compose_args(project, recovery_mode), "down", "--remove-orphans", "--volumes",
           env=compose_env, timeout=180)
    if recovery_mode and env_file is not None:
        env_file.unlink(missing_ok=True)


def inventory(env=None):
    grouped = defaultdict(list)
    for row in containers(env=env):
        project = row.get(PROJECT_LABEL, "")
        if PATTERN.fullmatch(project):
            grouped[project].append(row)
    result = []
    for project, rows in sorted(grouped.items()):
        tokens = {r.get(TOKEN_LABEL) for r in rows}
        expiries = {r.get(EXPIRY_LABEL) for r in rows}
        modes = {r.get(MODE_LABEL) for r in rows}
        if modes == {None}:
            mode = "default"
        elif modes == {RECOVERY_MODE}:
            mode = RECOVERY_MODE
        else:
            mode = "invalid"
        managed = (len(tokens) == 1 and len(expiries) == 1
                   and next(iter(tokens)) is not None
                   and next(iter(expiries)) is not None
                   and mode != "invalid")
        entry = {
            "project": project, "containers": len(rows), "managed": managed,
            "expires": next(iter(expiries)) if managed else None,
            "token": next(iter(tokens)) if managed else None,
        }
        # Preserve the default audit output shape for existing consumers.
        if mode == RECOVERY_MODE:
            entry["mode"] = mode
        result.append(entry)
    return result


def recovery_resource_inventory(env=None):
    resource_grouped = defaultdict(list)
    for kind in ("volume", "network"):
        for resource in inspect_resources(kind, recovery_mode=True, env=env):
            labels = resource["Labels"]
            project = labels.get(PROJECT_LABEL, "")
            if PATTERN.fullmatch(project):
                resource_grouped[project].append((kind, resource))
    result = []
    for project, resources in sorted(resource_grouped.items()):
        tokens = {row["Labels"].get(TOKEN_LABEL) for _, row in resources}
        expiries = {row["Labels"].get(EXPIRY_LABEL) for _, row in resources}
        valid_names = all(
            row.get("Name") in {f"{project}_{part}" for part in
                                 (VOLUMES if kind == "volume" else NETWORKS)}
            and row["Labels"].get(MODE_LABEL) == RECOVERY_MODE
            for kind, row in resources)
        token = next(iter(tokens)) if len(tokens) == 1 else None
        expiry = next(iter(expiries)) if len(expiries) == 1 else None
        managed = (valid_names and len(tokens) == 1 and bool(re.fullmatch(r"[0-9a-f]{32}", token or ""))
                   and len(expiries) == 1 and bool(expiry))
        result.append({
            "project": project, "containers": 0, "resources": len(resources),
            "managed": managed, "expires": expiry if managed else None,
            "token": token if managed else None, "mode": RECOVERY_MODE,
        })
    return result


def sweep(apply=False, recovery_only=False, env=None):
    now = int(time.time())
    rows = inventory(env=env)
    known_projects = {row["project"] for row in rows}
    rows.extend(row for row in recovery_resource_inventory(env=env)
                if row["project"] not in known_projects)
    if recovery_only:
        rows = [row for row in rows if row.get("mode") == RECOVERY_MODE]
    for row in rows:
        if not row["managed"]:
            continue
        try:
            expiry = int(row["expires"])
        except (TypeError, ValueError):
            continue
        if expiry > now:
            continue
        print(f"Expired managed fixture: {row['project']} ({row['containers']} containers)")
        if apply:
            if row.get("mode") == RECOVERY_MODE:
                cleanup(row["project"], row["token"], env=env, recovery_mode=True)
            elif not recovery_only:
                cleanup(row["project"], row["token"])
            print("Removed only token-verified disposable fixture")


def choose_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def fixture_environment(project, token, expiry, port, recovery_mode=False, *,
                       image_ref=None, image_id=None, docker_context=None,
                       docker_endpoint=None):
    if recovery_mode:
        if (not IMAGE_REF_PATTERN.fullmatch(image_ref or "")
                or not IMAGE_ID_PATTERN.fullmatch(image_id or "")
                or not DOCKER_CONTEXT_PATTERN.fullmatch(docker_context or "")
                or normalize_local_docker_endpoint(docker_endpoint) != docker_endpoint):
            raise ValueError("Recovery mode requires admitted local context and image identities")
        env = controlled_docker_environment(context_name=docker_context)
    else:
        env = os.environ.copy()
    # Never reuse production credentials for isolated synthetic test data.
    env.update({
        "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_PROFILES": "fixture",
        "MJOBS_PORT": str(port),
        "MJOBS_OPERATION_MODE": "FIXTURE",
        "MJOBS_COMPOSE_MOCK": "1",
        "MJOBS_DEV_GUARD_TOKEN": token,
        "MJOBS_DEV_GUARD_EXPIRES": str(expiry),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "MJOBS_CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(
            secrets.token_bytes(32)).decode("ascii"),
    })
    if recovery_mode:
        env.update(RECOVERY_ENV_VALUES)
        env.update(recovery_synthetic_credentials(token))
        env["MJOBS_IMAGE"] = image_id
        env["MJOBS_RECOVERY_IMAGE_REF"] = image_ref
        env["MJOBS_RECOVERY_IMAGE_ID"] = image_id
        env["MJOBS_RECOVERY_DOCKER_CONTEXT"] = docker_context
        env["MJOBS_RECOVERY_DOCKER_ENDPOINT"] = docker_endpoint
        env.setdefault("MJOBS_BROWSER_PROFILE", "")
        env.setdefault("MJOBS_WORKER_LOG_LEVEL", "WARNING")
        env.setdefault("MJOBS_SUBMISSION_HOURLY_LIMIT", "3")
        env.setdefault("MJOBS_SUBMISSION_DAILY_LIMIT", "10")
        env.setdefault("MJOBS_SUBMISSION_COMPANY_LIMIT", "1")
        env.setdefault("MJOBS_SOURCE_COLLECTION_CONCURRENCY", "2")
        env.setdefault("MJOBS_COMPANY_DISCOVERY_CONCURRENCY", "1")
        env.setdefault("MJOBS_AI_CONCURRENCY", "1")
        env.setdefault("MJOBS_DOCUMENT_GENERATION_CONCURRENCY", "1")
        env.setdefault("MJOBS_APPLICATION_AUTOMATION_CONCURRENCY", "1")
        env.setdefault("MJOBS_TRANSIENT_RETENTION_CONFIG", "")
        env.setdefault("MJOBS_TRANSIENT_RETENTION_INTERVAL_SECONDS", "86400")
        env.setdefault("MJOBS_MOCK_SCENARIO", "baseline")
    # Explicit -f flags are used; never inherit a caller's other Compose stack.
    env.pop("COMPOSE_FILE", None)
    if recovery_mode:
        env["COMPOSE_FILE"] = os.pathsep.join(compose_files(recovery_mode=True))
        env["COMPOSE_ENV_FILES"] = str(recovery_env_path(project))
    return env


def spawn_watchdog(project, token, expiry, recovery_mode=False):
    log_dir = ROOT / "outputs" / "dev-fixture-guard"
    log_dir.mkdir(parents=True, exist_ok=True)
    output = open(log_dir / f"{project}.watchdog.log", "ab")
    kwargs = {"cwd": ROOT, "stdin": subprocess.DEVNULL,
              "stdout": output, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    command = [sys.executable, "-m", "tools.dev_fixture_guard", "watchdog",
               project, token, str(expiry)]
    if recovery_mode:
        command.append("--recovery")
        path = recovery_env_path(project)
        recovery_environment_file(project, token, expiry)
        values = read_recovery_dotenv(path)
        watchdog_env = controlled_docker_environment(
            context_name=values.get("MJOBS_RECOVERY_DOCKER_CONTEXT"))
        watchdog_env["COMPOSE_FILE"] = os.pathsep.join(compose_files(recovery_mode=True))
        watchdog_env["COMPOSE_ENV_FILES"] = str(path)
        kwargs["env"] = watchdog_env
    try:
        return subprocess.Popen(
            command, **kwargs)
    finally:
        output.close()


def run(args):
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("Supply a verification command after --")
    recovery_mode = bool(getattr(args, "recovery", False))
    image_ref = getattr(args, "image_ref", None)
    image_id = getattr(args, "image_id", None)
    docker_context = getattr(args, "docker_context", None)
    expected_endpoint = None
    recovery_docker_env = None
    if recovery_mode:
        if image_ref is None or image_id is None or docker_context is None:
            raise ValueError("Recovery mode requires --image-ref, --image-id and --docker-context")
        _, expected_endpoint, recovery_docker_env = validate_local_docker_context(docker_context)
        validate_local_campaign_image(image_ref, image_id, recovery_docker_env)
        # Recovery cleanup is scoped to the explicitly admitted local engine.
        sweep(apply=True, recovery_only=True, env=recovery_docker_env)
    else:
        if any(value is not None for value in (image_ref, image_id, docker_context)):
            raise ValueError("Image and Docker context selection are recovery-only options")
        # Expired fixtures owned by this utility are safely reaped on the next run.
        sweep(apply=True)
    running = containers(running=True, env=recovery_docker_env)
    existing = {row.get(PROJECT_LABEL) for row in running
                if PATTERN.fullmatch(row.get(PROJECT_LABEL, ""))}
    if len(existing) >= args.max_fixtures:
        raise RuntimeError(
            f"Fixture limit reached ({len(existing)} running, "
            f"limit {args.max_fixtures}); inspect audit before starting another")
    project = f"mjobs-fixture-{secrets.token_hex(6)}"
    if any(project_resources(project, env=recovery_docker_env).values()):
        raise RuntimeError("Generated fixture identity collides with existing resources")
    token = secrets.token_hex(16)
    expiry = int(time.time()) + args.ttl_minutes * 60
    port = args.port or choose_port()
    env = fixture_environment(project, token, expiry, port, recovery_mode=recovery_mode,
                              image_ref=image_ref, image_id=image_id,
                              docker_context=docker_context,
                              docker_endpoint=expected_endpoint)
    if recovery_mode:
        recovery_environment_file(project, token, expiry, environment=env)
    try:
        watchdog = spawn_watchdog(project, token, expiry, recovery_mode=recovery_mode)
    except Exception:
        if recovery_mode:
            cleanup(project, token, env=env, recovery_mode=True)
        raise
    cleaned = False
    try:
        print(f"Starting disposable {project} on 127.0.0.1:{port}", flush=True)
        build_flag = "--build" if args.build else "--no-build"
        if recovery_mode:
            validate_local_docker_context(docker_context, expected_endpoint=expected_endpoint,
                                          environment=env)
            validate_local_campaign_image(image_ref, image_id, env)
        docker(*compose_args(project, recovery_mode), "up", "--detach", "--wait",
               "--wait-timeout", "180", build_flag, env=env, timeout=600)
        # Keep the bounded verification command attached. This process's exit
        # status is preserved, including a timeout or interrupted session.
        result = subprocess.run(command, cwd=ROOT, env=env,
                                timeout=args.ttl_minutes * 60, check=False)
        return result.returncode
    finally:
        try:
            cleanup(project, token, env=env, recovery_mode=recovery_mode)
            cleaned = True
            print(f"Disposed fixture {project}", flush=True)
        except (RuntimeError, ValueError) as exc:
            print(f"Cleanup not proven; watchdog/sweep can retry: {exc}",
                  file=sys.stderr, flush=True)
        if cleaned and watchdog.poll() is None:
            watchdog.terminate()


def watchdog(project, token, expiry, recovery_mode=False):
    seconds = int(expiry) - int(time.time())
    if seconds > 0:
        time.sleep(seconds)
    try:
        cleanup(project, token, recovery_mode=recovery_mode)
    except (RuntimeError, ValueError) as exc:
        # Normal foreground completion has already removed the project.
        print(f"Watchdog cleanup skipped: {exc}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    subs.add_parser("audit", help="Read-only inventory of fixture projects")
    s = subs.add_parser("sweep", help="Show/remove expired, tool-owned projects only")
    s.add_argument("--apply", action="store_true")
    r = subs.add_parser("run", help="Run a bounded disposable Docker fixture")
    r.add_argument("--ttl-minutes", type=int, default=60)
    r.add_argument("--max-fixtures", type=int, default=1)
    r.add_argument("--port", type=int)
    r.add_argument("--build", action="store_true")
    r.add_argument("--recovery", action="store_true",
                   help="Use the separately bounded restart-count recovery overlay")
    r.add_argument("--image-ref", help="Recovery-only local tagged campaign image to admit")
    r.add_argument("--image-id", help="Recovery-only immutable sha256 image ID from the build receipt")
    r.add_argument("--docker-context", help="Recovery-only local Docker context to bind")
    r.add_argument("command", nargs=argparse.REMAINDER)
    w = subs.add_parser("watchdog", help=argparse.SUPPRESS)
    w.add_argument("project")
    w.add_argument("token")
    w.add_argument("expiry", type=int)
    w.add_argument("--recovery", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "audit":
        for entry in inventory():
            print(json.dumps({k: v for k, v in entry.items() if k != "token"}))
        return 0
    if args.action == "sweep":
        sweep(apply=args.apply)
        return 0
    if args.action == "watchdog":
        watchdog(args.project, args.token, args.expiry, recovery_mode=args.recovery)
        return 0
    if not 1 <= args.ttl_minutes <= 240:
        parser.error("TTL must be 1-240 minutes")
    if not 1 <= args.max_fixtures <= 3:
        parser.error("Fixture limit must be 1-3")
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error("Port must be 1024-65535")
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Fixture guard: {exc}", file=sys.stderr)
        sys.exit(1)
