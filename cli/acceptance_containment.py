"""Operator-owned, fail-closed containment for local public-source observation.

The local absent-only claim serializes cooperating operators. The public source
API has no revision/CAS, so this module cannot prove a server-side lock or stop
an uncooperative manual client. It checks that boundary before admission and at
every callback, pauses only the exact scheduler container in the named fixture
project, and deliberately retains the pause and claim on every uncertain path.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

from cli.acceptance_public_source import ENDPOINTS, FINGERPRINT_FIELDS, POLICY_FIELDS

API_TIMEOUT_SECONDS = 15
PROJECT_PATTERN = re.compile(r"mjobs-fixture-[a-f0-9]{12}\Z")
CONTAINER_ID_PATTERN = re.compile(r"[a-f0-9]{12,64}\Z")
IMAGE_ID_PATTERN = re.compile(r"sha256:[a-f0-9]{64}\Z")
ACTIVE_COLLECTION_STATES = {"QUEUED", "RUNNING", "RETRY_WAIT"}
SUPPORTED_STAGES = {
    "preflight",
    "enable",
    "dispatch",
    "poll",
    "read",
    "finalize",
    "cleanup",
    "release",
}
CLAIM_NAME = "public-source-containment.json"
_INSPECT_LABELS = (
    "com.docker.compose.project",
    "com.docker.compose.service",
    "com.docker.compose.project.config_files",
    "com.docker.compose.project.working_dir",
    "org.opencontainers.image.source",
    "org.opencontainers.image.revision",
    "org.opencontainers.image.source-tree",
)
CONTAINER_INSPECT_TEMPLATE = "|".join(
    (
        "{{.Id}}",
        "{{.Image}}",
        "{{.Config.Image}}",
        "{{.Config.WorkingDir}}",
        *('{{index .Config.Labels "' + key + '"}}' for key in _INSPECT_LABELS),
        "{{.State.Status}}",
        "{{.State.Paused}}",
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",
        "{{json .NetworkSettings.Ports}}",
    )
)


class ContainmentError(RuntimeError):
    """Raised when local fixture ownership or source exclusivity is uncertain."""


def _require(condition, code):
    if not condition:
        raise ContainmentError(code)


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _timestamp(value):
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return parsed.astimezone(UTC)


def _has_reparse_point(path):
    candidate = Path(path)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        junction_check = getattr(current, "is_junction", None)
        if current.is_symlink() or (junction_check is not None and junction_check()):
            return True
    return False


def _run_docker(arguments, *, timeout=10):
    """Run Docker with a bounded, allowlisted command and no environment dump."""
    try:
        completed = subprocess.run(
            ["docker", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContainmentError("DOCKER_COMMAND_UNAVAILABLE") from error
    if (
        completed.returncode != 0
        or len(completed.stdout.encode("utf-8", errors="replace")) > 256 * 1024
    ):
        raise ContainmentError("DOCKER_COMMAND_FAILED")
    return completed.stdout.strip()


class DockerCLI:
    """Minimal Docker adapter; inspect output intentionally excludes Config.Env."""

    def list_service_containers(self, project, service):
        output = _run_docker(
            [
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=" + project,
                "--filter",
                "label=com.docker.compose.service=" + service,
            ]
        )
        return [line for line in output.splitlines() if line]

    def inspect(self, container_id):
        if (
            not isinstance(container_id, str)
            or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
        ):
            raise ContainmentError("CONTAINER_ID_INVALID")
        output = _run_docker(
            ["inspect", "--format", CONTAINER_INSPECT_TEMPLATE, container_id]
        )
        fields = output.split("|", len(_INSPECT_LABELS) + 7)
        if len(fields) != len(_INSPECT_LABELS) + 8:
            raise ContainmentError("CONTAINER_INSPECTION_INVALID")
        try:
            labels = dict(zip(_INSPECT_LABELS, fields[4 : 4 + len(_INSPECT_LABELS)]))
            state_index = 4 + len(_INSPECT_LABELS)
            result = {
                "id": fields[0],
                "image_id": fields[1],
                "image_ref": fields[2],
                "working_dir": fields[3],
                "labels": labels,
                "status": fields[state_index],
                "paused": fields[state_index + 1] == "true",
                "health": fields[state_index + 2],
                "ports": json.loads(fields[state_index + 3]),
            }
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ContainmentError("CONTAINER_INSPECTION_INVALID") from error
        return result

    def pause(self, container_id):
        _run_docker(["pause", container_id])

    def unpause(self, container_id):
        _run_docker(["unpause", container_id])


class OwnedPublicContainment:
    """Callback and explicit cleanup guard for one isolated Compose project.

    Pass ``instance`` as ``containment_preflight`` and ``instance.request`` as
    ``request`` to ``cli.acceptance_public_source.run``. The claim is created
    only with exclusive-create semantics under an ignored ``outputs`` run dir.
    It contains an operator token and is intentionally retained after failures.
    A passing final baseline and source cleanup must be followed by an explicit
    call at the runner's explicit ``release`` stage. That stage is accepted only
    after the runner reports its helper cleanup state and this class independently
    verifies the unchanged fixture baseline.
    """

    def __init__(
        self,
        *,
        project_name,
        base_url,
        source_label,
        image_ref,
        image_id,
        source_revision,
        source_tree,
        compose_working_dir,
        container_working_dir,
        compose_files,
        run_dir,
        baseline,
        request=None,
        docker=None,
        owner,
        repository_root=None,
        lease_seconds=60,
        utc_now=lambda: datetime.now(UTC),
        sleep=time.sleep,
    ):
        if (
            not isinstance(project_name, str)
            or PROJECT_PATTERN.fullmatch(project_name) is None
            or not isinstance(source_label, str)
            or not source_label.startswith("https://")
            or "|" in source_label
            or not isinstance(image_ref, str)
            or not image_ref
            or "|" in image_ref
            or not isinstance(image_id, str)
            or IMAGE_ID_PATTERN.fullmatch(image_id) is None
            or not isinstance(source_revision, str)
            or re.fullmatch(r"[a-f0-9]{40}", source_revision) is None
            or not isinstance(source_tree, str)
            or re.fullmatch(r"[a-f0-9]{40}", source_tree) is None
            or not isinstance(owner, str)
            or not owner.strip()
            or isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 16 <= lease_seconds <= 600
            or not isinstance(baseline, dict)
        ):
            raise ValueError("CONTAINMENT_CONFIGURATION_INVALID")
        url = urlsplit(base_url) if isinstance(base_url, str) else None
        if (
            url is None
            or url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.port is None
            or not 1024 <= url.port <= 65535
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("CONTAINMENT_API_URL_INVALID")
        if (
            not isinstance(baseline.get("profile_id"), str)
            or not baseline["profile_id"]
            or not isinstance(baseline.get("application_id"), str)
            or not baseline["application_id"]
            or baseline.get("external_submission_enabled") is not False
        ):
            raise ValueError("CONTAINMENT_BASELINE_INVALID")

        self.project_name = project_name
        self.base_url = base_url.rstrip("/")
        self.api_port = url.port
        self.api_host = url.hostname
        self.source_label = source_label
        self.image_ref = image_ref
        self.image_id = image_id
        self.source_revision = source_revision
        self.source_tree = source_tree
        self.repository_root = Path(
            repository_root or Path(__file__).resolve().parents[1]
        ).resolve(strict=True)
        self.compose_working_dir = Path(compose_working_dir).resolve(strict=True)
        if self.compose_working_dir != self.repository_root:
            raise ValueError("CONTAINMENT_WORKTREE_MISMATCH")
        if (
            not isinstance(container_working_dir, str)
            or not container_working_dir.startswith("/")
            or ".." in Path(container_working_dir).parts
        ):
            raise ValueError("CONTAINMENT_CONTAINER_WORKDIR_INVALID")
        self.container_working_dir = container_working_dir
        self.compose_files = self._resolve_compose_files(compose_files)
        self.compose_config_files_label = ",".join(
            str(path) for path in self.compose_files
        )
        self.run_dir = self._validate_run_dir(run_dir)
        self.claim_path = self.run_dir / CLAIM_NAME
        self.baseline = dict(baseline)
        self.owner = owner.strip()
        self.token = secrets.token_urlsafe(32)
        self.lease_seconds = lease_seconds
        self.utc_now = utc_now
        self.sleep = sleep
        self.sleep_intervals = []
        self.docker = docker or DockerCLI()
        self._injected_request = request
        self._api_id = None
        self._scheduler_id = None
        self._original_scheduler_status = None
        self._paused_by_us = False
        self._claim_created = False
        self._admitted = False
        self._released = False
        self._lease_expiry = None
        self._source = None
        self._source_created = False
        self._source_preflight_seen = False
        self._source_preflight_present = None
        self._source_cleanup_started = False
        self._enable_source_id = None
        self._own_request_id = None
        self._own_profile_id = None
        self._own_operation_id = None
        self._collection_attempted = False
        self._enable_attempted = False
        self._disable_attempted = False
        self._last_stage = None

    def _resolve_compose_files(self, values):
        if not isinstance(values, (tuple, list)) or not values:
            raise ValueError("CONTAINMENT_COMPOSE_FILES_INVALID")
        resolved = []
        for value in values:
            path = Path(value)
            if not path.is_absolute():
                path = self.repository_root / path
            if _has_reparse_point(path):
                raise ValueError("CONTAINMENT_COMPOSE_FILE_REPARSE_POINT")
            path = path.resolve(strict=True)
            try:
                path.relative_to(self.repository_root)
            except ValueError as error:
                raise ValueError("CONTAINMENT_COMPOSE_FILE_OUTSIDE_WORKTREE") from error
            if not path.is_file() or "," in str(path):
                raise ValueError("CONTAINMENT_COMPOSE_FILE_INVALID")
            resolved.append(path)
        if len({os.path.normcase(str(path)) for path in resolved}) != len(resolved):
            raise ValueError("CONTAINMENT_COMPOSE_FILES_DUPLICATED")
        root_compose = self.repository_root / "compose.yml"
        if not resolved or os.path.normcase(str(resolved[0])) != os.path.normcase(
            str(root_compose.resolve(strict=True))
        ):
            raise ValueError("CONTAINMENT_COMPOSE_ORDER_INVALID")
        return tuple(resolved)

    def _validate_run_dir(self, value):
        root_outputs = self.repository_root / "outputs"
        if not root_outputs.is_dir() or _has_reparse_point(root_outputs):
            raise ValueError("CONTAINMENT_OUTPUT_DIR_INVALID")
        path = Path(value)
        if not path.is_absolute():
            path = self.repository_root / path
        if _has_reparse_point(path):
            raise ValueError("CONTAINMENT_OUTPUT_DIR_INVALID")
        path = path.resolve(strict=True)
        try:
            path.relative_to(root_outputs.resolve(strict=True))
        except ValueError as error:
            raise ValueError(
                "CONTAINMENT_OUTPUT_DIR_OUTSIDE_IGNORED_OUTPUTS"
            ) from error
        if not path.is_dir():
            raise ValueError("CONTAINMENT_OUTPUT_DIR_INVALID")
        relative = path.relative_to(self.repository_root).as_posix()
        try:
            check = subprocess.run(
                ["git", "check-ignore", "--quiet", "--", relative],
                cwd=self.repository_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("CONTAINMENT_OUTPUT_IGNORE_CHECK_FAILED") from error
        if check.returncode != 0:
            raise ValueError("CONTAINMENT_OUTPUT_DIR_NOT_IGNORED")
        return path

    def _docker_list(self, service):
        try:
            values = self.docker.list_service_containers(self.project_name, service)
        except Exception as error:
            raise ContainmentError("PROJECT_CONTAINER_LIST_FAILED") from error
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], str)
        ):
            raise ContainmentError("PROJECT_CONTAINER_TARGET_AMBIGUOUS")
        if CONTAINER_ID_PATTERN.fullmatch(values[0]) is None:
            raise ContainmentError("CONTAINER_ID_INVALID")
        return values[0]

    def _expected_labels(self, service):
        return {
            "com.docker.compose.project": self.project_name,
            "com.docker.compose.service": service,
            "com.docker.compose.project.config_files": self.compose_config_files_label,
            "com.docker.compose.project.working_dir": str(self.compose_working_dir),
            "org.opencontainers.image.source": self.source_label,
            "org.opencontainers.image.revision": self.source_revision,
            "org.opencontainers.image.source-tree": self.source_tree,
        }

    def _now_utc(self):
        value = self.utc_now()
        _require(
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() == timedelta(0),
            "CONTAINMENT_CLOCK_INVALID",
        )
        return value.astimezone(UTC)

    def _inspect(self, container_id):
        try:
            row = self.docker.inspect(container_id)
        except Exception as error:
            raise ContainmentError("PROJECT_CONTAINER_INSPECT_FAILED") from error
        if not isinstance(row, dict):
            raise ContainmentError("CONTAINER_INSPECTION_INVALID")
        return row

    def _verify_container(
        self, service, container_id, *, required_status, required_paused
    ):
        expected_id = self._docker_list(service)
        _require(expected_id == container_id, "PROJECT_CONTAINER_REPLACED")
        row = self._inspect(container_id)
        _require(row.get("id") == container_id, "PROJECT_CONTAINER_ID_CHANGED")
        _require(
            row.get("image_id") == self.image_id
            and row.get("image_ref") == self.image_ref,
            "PROJECT_IMAGE_CHANGED",
        )
        _require(
            row.get("working_dir") == self.container_working_dir,
            "PROJECT_CONTAINER_WORKDIR_CHANGED",
        )
        labels = row.get("labels")
        expected = self._expected_labels(service)
        _require(
            isinstance(labels, dict)
            and all(labels.get(key) == value for key, value in expected.items()),
            "PROJECT_CONTAINER_LABELS_CHANGED",
        )
        _require(
            row.get("status") == required_status
            and row.get("paused") is required_paused,
            "PROJECT_CONTAINER_STATE_CHANGED",
        )
        _require(row.get("health") == "healthy", "PROJECT_CONTAINER_UNHEALTHY")
        if service == "api":
            ports = row.get("ports") or {}
            bindings = ports.get("8000/tcp")
            _require(
                isinstance(bindings, list)
                and len(bindings) == 1
                and bindings[0].get("HostIp") in {"127.0.0.1", "::1"}
                and bindings[0].get("HostPort") == str(self.api_port),
                "PROJECT_API_BINDING_CHANGED",
            )
            if self.api_host == "127.0.0.1":
                _require(
                    bindings[0].get("HostIp") == "127.0.0.1",
                    "PROJECT_API_BINDING_CHANGED",
                )
            elif self.api_host == "::1":
                _require(
                    bindings[0].get("HostIp") == "::1", "PROJECT_API_BINDING_CHANGED"
                )
            _require(
                isinstance(ports, dict)
                and all(
                    not value for target, value in ports.items() if target != "8000/tcp"
                ),
                "PROJECT_API_BINDING_CHANGED",
            )
        return row

    def _verify_targets(self, *, pre_admission=False):
        api_id = self._docker_list("api")
        scheduler_id = self._docker_list("scheduler")
        if self._api_id is not None:
            _require(api_id == self._api_id, "PROJECT_API_REPLACED")
        if self._scheduler_id is not None:
            _require(scheduler_id == self._scheduler_id, "PROJECT_SCHEDULER_REPLACED")
        self._api_id, self._scheduler_id = api_id, scheduler_id
        self._verify_container(
            "api", api_id, required_status="running", required_paused=False
        )
        scheduler = self._inspect(scheduler_id)
        labels = scheduler.get("labels")
        _require(scheduler.get("id") == scheduler_id, "PROJECT_SCHEDULER_ID_CHANGED")
        _require(
            scheduler.get("image_id") == self.image_id
            and scheduler.get("image_ref") == self.image_ref,
            "PROJECT_IMAGE_CHANGED",
        )
        _require(
            scheduler.get("working_dir") == self.container_working_dir,
            "PROJECT_CONTAINER_WORKDIR_CHANGED",
        )
        expected = self._expected_labels("scheduler")
        _require(
            isinstance(labels, dict)
            and all(labels.get(key) == value for key, value in expected.items()),
            "PROJECT_CONTAINER_LABELS_CHANGED",
        )
        _require(scheduler.get("health") == "healthy", "PROJECT_CONTAINER_UNHEALTHY")
        if pre_admission:
            _require(
                scheduler.get("status") == "running"
                and scheduler.get("paused") is False,
                "SCHEDULER_ORIGINAL_STATE_UNSAFE",
            )
            self._original_scheduler_status = scheduler["status"]
        else:
            _require(
                self._paused_by_us
                and scheduler.get("status") == "paused"
                and scheduler.get("paused") is True,
                "OWNED_SCHEDULER_NOT_PAUSED",
            )
        return scheduler

    def _validate_api_path(self, path):
        if not isinstance(path, str):
            raise ContainmentError("API_PATH_INVALID")
        parsed = urlsplit(path)
        decoded_parts = [part for part in parsed.path.split("/") if part]
        if (
            not path.startswith("/")
            or path.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or any(
                unquote(part) in {".", ".."}
                or "/" in unquote(part)
                or "\\" in unquote(part)
                for part in decoded_parts
            )
        ):
            raise ContainmentError("API_PATH_INVALID")

    def request(self, method, path, **kwargs):
        """Use the configured loopback API transport and record our single collection."""
        self._validate_api_path(path)
        if method not in {"GET", "POST", "PUT"}:
            raise ContainmentError("API_METHOD_INVALID")
        if method == "PUT":
            _require(path == "/sources" and self._admitted, "API_WRITE_SCOPE_INVALID")
            self._verify_targets()
            self._ensure_lease()
            self._verify_api()
            _require(not self._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")
            body = kwargs.get("json")
            current_sources = self._source_rows()
            _require(
                isinstance(body, dict) and set(body) == set(POLICY_FIELDS),
                "SOURCE_WRITE_INVALID",
            )
            if self._last_stage == "enable":
                _require(
                    not self._enable_attempted
                    and self._source is None
                    and not current_sources
                    and body
                    == {
                        "provider": self._provider,
                        "tenant": "default",
                        "enabled": True,
                        "poll_interval": 604800,
                        "concurrency": 1,
                        "request_timeout": 10,
                        "rate_limit": 1,
                        "jitter_seconds": 0,
                    },
                    "SOURCE_ENABLE_SCOPE_INVALID",
                )
                self._enable_attempted = True
            elif self._last_stage == "cleanup":
                expected = (
                    {key: self._source.get(key) for key in POLICY_FIELDS}
                    if self._source
                    else None
                )
                _require(
                    self._source_created
                    and self._source_cleanup_started
                    and not self._disable_attempted
                    and len(current_sources) == 1
                    and self._policy_digest(current_sources[0])
                    == self._policy_digest(self._source)
                    and isinstance(expected, dict)
                    and body == {**expected, "enabled": False},
                    "SOURCE_DISABLE_SCOPE_INVALID",
                )
                self._disable_attempted = True
            else:
                raise ContainmentError("SOURCE_WRITE_STAGE_INVALID")
        elif method == "POST" and not (
            path.startswith("/sources/") and path.endswith("/collect")
        ):
            raise ContainmentError("API_WRITE_SCOPE_INVALID")
        if (
            method == "POST"
            and path.startswith("/sources/")
            and path.endswith("/collect")
        ):
            source_segment = path.split("/")[2]
            source_id = unquote(source_segment)
            _require(
                source_id
                and "/" not in source_id
                and "\\" not in source_id
                and quote(source_id, safe="") == source_segment,
                "COLLECTION_SOURCE_ID_INVALID",
            )
            _require(
                self._admitted and not self._released,
                "CONTAINMENT_REQUIRED_BEFORE_COLLECTION",
            )
            self._verify_targets()
            self._ensure_lease()
            self._verify_api()
            self._source_policy(
                {
                    "provider": self._provider,
                    "base_url": self.base_url,
                    "source_id": source_id,
                    "source_policy_sha256": None,
                    "newly_created": self._source_created,
                },
                "dispatch",
            )
            _require(not self._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")
            _require(not self._collection_attempted, "COLLECTION_RETRY_FORBIDDEN")
            self._collection_attempted = True
            body = kwargs.get("json")
            _require(
                isinstance(body, dict)
                and isinstance(body.get("request_id"), str)
                and isinstance(body.get("profile_id"), str),
                "COLLECTION_REQUEST_INVALID",
            )
            self._own_request_id = body["request_id"]
            self._own_profile_id = body["profile_id"]
            self._own_source_id = source_id
        if self._injected_request is not None:
            response = self._injected_request(method, path, **kwargs)
        else:
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    timeout=API_TIMEOUT_SECONDS,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    result = client.request(method, path, **kwargs)
                    result.raise_for_status()
                    response = result.json()
            except Exception as error:
                raise ContainmentError("LOOPBACK_API_REQUEST_FAILED") from error
        if (
            method == "POST"
            and path.startswith("/sources/")
            and path.endswith("/collect")
            and isinstance(response, dict)
            and isinstance(response.get("operation_id"), str)
        ):
            self._own_operation_id = response["operation_id"]
        if method == "PUT" and self._last_stage == "enable" and self._source is None:
            _require(
                isinstance(response, dict)
                and isinstance(response.get("id"), str)
                and response["id"],
                "SOURCE_ENABLE_RESPONSE_INVALID",
            )
            self._source_created = True
            self._enable_source_id = response["id"]
        return response

    def _api_get(self, path):
        return self.request("GET", path)

    def _verify_api(self):
        settings = self._api_get("/settings")
        health = self._api_get("/health")
        _require(
            isinstance(settings, dict)
            and settings.get("operation_mode") == "FIXTURE"
            and settings.get("external_submission_enabled") is False,
            "FIXTURE_API_REQUIRED",
        )
        _require(
            isinstance(health, dict)
            and health.get("external_submission_enabled") is False,
            "EXTERNAL_SUBMISSION_MUST_REMAIN_DISABLED",
        )

    def _active_discoveries(self):
        response = self._api_get("/operations")
        _require(
            isinstance(response, dict) and isinstance(response.get("operations"), list),
            "OPERATIONS_RESPONSE_INVALID",
        )
        _require(len(response["operations"]) < 100, "OPERATIONS_INVENTORY_INCOMPLETE")
        active = []
        for row in response["operations"]:
            if not isinstance(row, dict):
                raise ContainmentError("OPERATIONS_RESPONSE_INVALID")
            if (
                row.get("kind") != "DISCOVER"
                or row.get("state") not in ACTIVE_COLLECTION_STATES
            ):
                continue
            params = row.get("params") if isinstance(row.get("params"), dict) else {}
            own = (
                self._own_operation_id is not None
                and row.get("id") == self._own_operation_id
                and row.get("entity_id") == self._own_profile_id
                and params.get("source_id") == getattr(self, "_own_source_id", None)
                and params.get("request_id") == self._own_request_id
            )
            if not own:
                active.append(row)
        return active

    def _policy_digest(self, source):
        return _digest({key: source[key] for key in FINGERPRINT_FIELDS})

    def _source_rows(self):
        response = self._api_get("/sources")
        _require(
            isinstance(response, dict) and isinstance(response.get("sources"), list),
            "SOURCE_RESPONSE_INVALID",
        )
        provider = getattr(self, "_provider", None)
        if provider is None:
            return response["sources"]
        rows = [
            row
            for row in response["sources"]
            if isinstance(row, dict)
            and row.get("provider") == provider
            and row.get("tenant") == "default"
        ]
        _require(len(rows) <= 1, "SOURCE_POLICY_AMBIGUOUS")
        return rows

    def _source_policy(self, context, stage):
        provider = context.get("provider") if isinstance(context, dict) else None
        base_url = context.get("base_url") if isinstance(context, dict) else None
        _require(
            provider in ENDPOINTS
            and isinstance(base_url, str)
            and base_url.rstrip("/") == self.base_url,
            "SOURCE_SCOPE_CHANGED",
        )
        self._provider = provider
        rows = self._source_rows()
        source = rows[0] if rows else None
        source_id = context.get("source_id") if isinstance(context, dict) else None
        expected_hash = (
            context.get("source_policy_sha256") if isinstance(context, dict) else None
        )
        newly_created = (
            context.get("newly_created") is True if isinstance(context, dict) else False
        )
        if stage == "preflight":
            present = source is not None
            if self._source_preflight_seen:
                _require(
                    present == self._source_preflight_present,
                    "SOURCE_POLICY_CHANGED_DURING_PREFLIGHT",
                )
            else:
                self._source_preflight_present = present
                self._source_preflight_seen = True
        if source is None:
            _require(not source_id and not expected_hash, "SOURCE_POLICY_DISAPPEARED")
            _require(not newly_created, "OWNED_SOURCE_DISAPPEARED")
            return
        _require(
            isinstance(source.get("id"), str)
            and source.get("provider") == provider
            and source.get("tenant") == "default",
            "SOURCE_IDENTITY_INVALID",
        )
        if source_id:
            _require(source.get("id") == source_id, "SOURCE_IDENTITY_CHANGED")
        if self._source is None:
            if self._source_preflight_present is False:
                _require(
                    newly_created
                    and self._enable_source_id is not None
                    and source.get("id") == self._enable_source_id,
                    "UNOWNED_SOURCE_APPEARED",
                )
            self._source = dict(source)
            self._source_created = newly_created
            if newly_created:
                _require(
                    self._enable_source_id is not None
                    and source.get("id") == self._enable_source_id,
                    "SOURCE_ENABLE_IDENTITY_CHANGED",
                )
                expected_policy = {
                    "provider": provider,
                    "tenant": "default",
                    "enabled": True,
                    "poll_interval": 604800,
                    "concurrency": 1,
                    "request_timeout": 10,
                    "rate_limit": 1,
                    "jitter_seconds": 0,
                }
                _require(
                    all(
                        source.get(key) == value
                        for key, value in expected_policy.items()
                    ),
                    "NEW_SOURCE_POLICY_UNSAFE",
                )
        else:
            _require(
                source.get("id") == self._source.get("id"), "SOURCE_IDENTITY_CHANGED"
            )
            expected = dict(self._source)
            if self._disable_attempted and self._source_created:
                expected["enabled"] = False
            _require(
                self._policy_digest(source) == self._policy_digest(expected),
                "SOURCE_POLICY_CHANGED",
            )
            if self._disable_attempted and self._source_created:
                _require(source.get("enabled") is False, "SOURCE_CLEANUP_UNCONFIRMED")
        if expected_hash:
            _require(
                isinstance(expected_hash, str)
                and re.fullmatch(r"[a-f0-9]{64}", expected_hash)
                and (
                    expected_hash == self._policy_digest(source)
                    or (
                        self._disable_attempted
                        and self._source_created
                        and expected_hash == self._policy_digest(self._source)
                    )
                ),
                "SOURCE_POLICY_FINGERPRINT_CHANGED",
            )
        if newly_created:
            _require(
                self._source_created and source.get("id") == source_id,
                "SOURCE_OWNERSHIP_MISMATCH",
            )
        if stage == "cleanup":
            _require(
                newly_created and self._source_created, "SOURCE_CLEANUP_SCOPE_INVALID"
            )
            self._source_cleanup_started = True

    def _check_claim_path(self):
        _require(
            self._claim_created
            and self.claim_path.exists()
            and not _has_reparse_point(self.claim_path),
            "CONTAINMENT_CLAIM_MISSING",
        )
        try:
            claim = json.loads(self.claim_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContainmentError("CONTAINMENT_CLAIM_INVALID") from error
        _require(
            isinstance(claim, dict)
            and claim.get("owner") == self.owner
            and claim.get("token") == self.token
            and claim.get("project") == self.project_name
            and claim.get("api_container_id") == self._api_id
            and claim.get("scheduler_container_id") == self._scheduler_id,
            "CONTAINMENT_OWNERSHIP_CHANGED",
        )
        try:
            self._lease_expiry = _timestamp(claim.get("lease_expires_at"))
        except (TypeError, ValueError):
            raise ContainmentError("CONTAINMENT_LEASE_INVALID") from None
        return claim

    def _write_claim(self, *, exclusive):
        now = self._now_utc()
        expiry = now + timedelta(seconds=self.lease_seconds)
        record = {
            "schema_version": 1,
            "owner": self.owner,
            "token": self.token,
            "project": self.project_name,
            "api_container_id": self._api_id,
            "scheduler_container_id": self._scheduler_id,
            "source_revision": self.source_revision,
            "source_tree": self.source_tree,
            "image_id": self.image_id,
            "base_url": self.base_url,
            "created_at": getattr(self, "_claim_created_at", now).isoformat(),
            "lease_expires_at": expiry.isoformat(),
            "lease_model": "LOCAL_OPERATOR_CLAIM_NO_SERVER_SIDE_CAS",
        }
        payload = (
            json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        if exclusive:
            try:
                descriptor = os.open(
                    self.claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError as error:
                raise ContainmentError("CLAIM_ALREADY_EXISTS") from error
            except OSError as error:
                raise ContainmentError("CONTAINMENT_CLAIM_CREATE_FAILED") from error
            self._claim_created = True
            self._claim_created_at = now
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as error:
                raise ContainmentError("CONTAINMENT_CLAIM_WRITE_FAILED") from error
        else:
            self._check_claim_path()
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".containment-lease-", dir=self.run_dir
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._check_claim_path()
                os.replace(temporary, self.claim_path)
            except OSError as error:
                raise ContainmentError("CONTAINMENT_LEASE_RENEW_FAILED") from error
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        self._lease_expiry = expiry

    def _ensure_lease(self):
        claim = self._check_claim_path()
        now = self._now_utc()
        _require(self._lease_expiry > now, "CONTAINMENT_LEASE_EXPIRED")
        if self._lease_expiry <= now + timedelta(seconds=API_TIMEOUT_SECONDS + 1):
            self._write_claim(exclusive=False)
            claim = self._check_claim_path()
        _require(
            self._lease_expiry
            > self._now_utc() + timedelta(seconds=API_TIMEOUT_SECONDS),
            "CONTAINMENT_LEASE_TOO_SHORT",
        )
        return claim

    def _create_claim(self):
        self._write_claim(exclusive=True)

    def _admit(self, context):
        _require(
            self._original_scheduler_status is None and not self._admitted,
            "CONTAINMENT_ALREADY_ADMITTED",
        )
        self._verify_targets(pre_admission=True)
        self._verify_api()
        self._provider = context.get("provider") if isinstance(context, dict) else None
        _require(
            self._provider in ENDPOINTS and context.get("base_url") == self.base_url,
            "SOURCE_SCOPE_CHANGED",
        )
        self._source_policy(context, "preflight")
        _require(not self._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")
        self._create_claim()
        # Recheck both exact labeled targets after claim creation and before pause.
        self._verify_targets(pre_admission=True)
        self._verify_api()
        _require(not self._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")
        try:
            self.docker.pause(self._scheduler_id)
        except Exception as error:
            raise ContainmentError("OWNED_SCHEDULER_PAUSE_FAILED") from error
        self._paused_by_us = True
        self._admitted = True
        self._verify_targets()
        self._ensure_lease()
        self._verify_api()
        self._source_policy(context, "preflight")
        _require(not self._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")

    def __call__(self, stage, context):
        if stage not in SUPPORTED_STAGES or not isinstance(context, dict):
            raise ContainmentError("CONTAINMENT_STAGE_INVALID")
        if self._released:
            raise ContainmentError("CONTAINMENT_ALREADY_RELEASED")
        self._last_stage = stage
        if not self._admitted:
            _require(stage == "preflight", "CONTAINMENT_PREFLIGHT_REQUIRED")
            self._admit(context)
        else:
            self._verify_targets()
            self._ensure_lease()
            self._verify_api()
            self._source_policy(context, stage)
            _require(not self._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")
        if stage == "release":
            self._release_after_baseline(context.get("cleanup_status"))
            return {
                "released": True,
                "scheduler_contained": False,
                "source_policy_exclusive": True,
                "source_collection_exclusive": False,
                "owner": self.owner,
                "token": self.token,
                "lease_expires_at": self._lease_expiry.isoformat(),
            }
        return {
            "scheduler_contained": True,
            "source_policy_exclusive": True,
            "source_collection_exclusive": True,
            "owner": self.owner,
            "token": self.token,
            "lease_expires_at": self._lease_expiry.isoformat(),
        }

    def _verify_baseline(self):
        self._verify_api()
        profile_id = quote(self.baseline["profile_id"], safe="")
        application_id = quote(self.baseline["application_id"], safe="")
        profile = self._api_get("/profiles/" + profile_id)
        app = self._api_get(
            "/profiles/" + profile_id + "/applications/" + application_id
        )
        _require(
            profile.get("id") == self.baseline["profile_id"]
            and app.get("id") == self.baseline["application_id"]
            and app.get("profile_id") == self.baseline["profile_id"]
            and app.get("target") == "MOCK",
            "BASELINE_SELECTION_CHANGED",
        )
        job_id = self.baseline.get("selected_application_job_id")
        route_id = self.baseline.get("selected_application_route_id")
        _require(
            isinstance(job_id, str)
            and isinstance(route_id, str)
            and app.get("job_id") == job_id
            and app.get("route_id") == route_id,
            "BASELINE_SELECTION_CHANGED",
        )
        if "confirmation" in self.baseline:
            _require(
                app.get("confirmation") == self.baseline["confirmation"],
                "BASELINE_SELECTION_CHANGED",
            )
        if "profile_version" in self.baseline:
            _require(
                profile.get("version") == self.baseline["profile_version"],
                "BASELINE_SELECTION_CHANGED",
            )
        job = self._api_get("/jobs/" + quote(job_id, safe=""))
        routes = [
            row
            for row in job.get("routes", [])
            if isinstance(row, dict) and row.get("id") == route_id
        ]
        _require(
            job.get("id") == job_id
            and (job.get("attributes") or {}).get("fictional") is True
            and len(routes) == 1
            and routes[0].get("provider") == "mock-ats",
            "BASELINE_SELECTION_CHANGED",
        )

    def _verify_source_cleanup(self):
        rows = self._source_rows()
        if self._source_created:
            _require(
                self._source_cleanup_started
                and self._disable_attempted
                and len(rows) == 1,
                "SOURCE_CLEANUP_NOT_CONFIRMED",
            )
            source = rows[0]
            expected = dict(self._source)
            expected["enabled"] = False
            _require(
                self._policy_digest(source) == self._policy_digest(expected),
                "SOURCE_CLEANUP_NOT_CONFIRMED",
            )
        elif self._source is not None:
            _require(
                len(rows) == 1
                and self._policy_digest(rows[0]) == self._policy_digest(self._source),
                "SOURCE_POLICY_CHANGED",
            )
        else:
            _require(not rows, "SOURCE_APPEARED_AFTER_PREFLIGHT")

    def _release_after_baseline(self, cleanup_status):
        """Explicitly release only after source cleanup and baseline proof.

        The fixed operator lease is waited out in intervals of at most 15 seconds.
        If the scheduler, claim, baseline, or active-operation fence changes, the
        pause and claim remain in place for manual diagnosis.
        """
        _require(
            self._admitted and not self._released and self._paused_by_us,
            "CONTAINMENT_RELEASE_NOT_AVAILABLE",
        )
        expected_cleanup = (
            "RESTORED_DISABLED" if self._source_created else "NOT_REQUIRED"
        )
        _require(cleanup_status == expected_cleanup, "SOURCE_CLEANUP_STATUS_INVALID")
        self._verify_targets()
        self._ensure_lease()
        self._verify_source_cleanup()
        self._verify_baseline()
        _require(not self._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")
        expiry = self._lease_expiry
        while True:
            self._verify_targets()
            self._check_claim_path()
            expiry = self._lease_expiry
            self._verify_source_cleanup()
            _require(not self._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")
            remaining = (expiry - self._now_utc()).total_seconds()
            if remaining <= 0:
                break
            interval = min(15.0, remaining)
            self.sleep_intervals.append(interval)
            self.sleep(interval)
        self._verify_targets()
        self._check_claim_path()
        self._verify_source_cleanup()
        self._verify_baseline()
        _require(not self._active_discoveries(), "ACTIVE_COLLECTION_EXISTS")
        _require(
            self._original_scheduler_status == "running" and self._paused_by_us,
            "SCHEDULER_ORIGINAL_STATE_UNSAFE",
        )
        try:
            self.docker.unpause(self._scheduler_id)
            self._verify_container(
                "scheduler",
                self._scheduler_id,
                required_status="running",
                required_paused=False,
            )
            self._check_claim_path()
            self.claim_path.unlink()
        except Exception as error:
            self._try_repause_owned_scheduler()
            if isinstance(error, ContainmentError):
                raise
            raise ContainmentError(
                "OWNED_SCHEDULER_UNPAUSE_OR_RELEASE_FAILED"
            ) from error
        self._released = True

    def _try_repause_owned_scheduler(self):
        """Best-effort rollback after a controlled post-unpause release failure."""
        if not self._scheduler_id:
            return
        try:
            if self._docker_list("scheduler") != self._scheduler_id:
                return
            row = self._inspect(self._scheduler_id)
            expected = self._expected_labels("scheduler")
            labels = row.get("labels")
            if (
                row.get("id") != self._scheduler_id
                or row.get("image_id") != self.image_id
                or row.get("image_ref") != self.image_ref
                or row.get("working_dir") != self.container_working_dir
                or not isinstance(labels, dict)
                or any(labels.get(key) != value for key, value in expected.items())
                or row.get("status") != "running"
                or row.get("paused") is not False
            ):
                return
            self.docker.pause(self._scheduler_id)
            row = self._inspect(self._scheduler_id)
            if (
                row.get("id") == self._scheduler_id
                and row.get("status") == "paused"
                and row.get("paused") is True
            ):
                self._paused_by_us = True
        except Exception:  # noqa: BLE001 - best-effort rollback must not broaden target scope
            # The durable claim remains; never broaden rollback to another ID.
            return
